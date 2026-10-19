targetScope = 'resourceGroup'

@description('Azure region for all Bastion VM resources.')
param location string

@description('Short lowercase prefix used in Bastion VM resource names. Use letters, numbers, and hyphens only.')
@minLength(3)
@maxLength(18)
param namePrefix string

@description('Local Linux bootstrap account name. Normal operator access uses Microsoft Entra SSH login.')
@minLength(1)
@maxLength(32)
param adminUsername string

@description('SSH public key for the required local Linux bootstrap account.')
@minLength(1)
param adminSshPublicKey string

@description('CIDR range for the Bastion VM VNet.')
param vnetAddressPrefix string

@description('CIDR range for Azure Bastion. Azure Bastion requires a dedicated subnet named AzureBastionSubnet with /26 or larger.')
param bastionSubnetPrefix string

@description('CIDR range for the Bastion VM VM subnet.')
param workstationSubnetPrefix string

@description('Bastion VM size.')
param vmSize string

@description('Managed OS disk SKU for the Bastion VM.')
@allowed([
  'Premium_LRS'
  'Premium_ZRS'
  'StandardSSD_LRS'
  'StandardSSD_ZRS'
])
param osDiskStorageAccountType string

@description('Ubuntu image publisher.')
param imagePublisher string

@description('Ubuntu image offer.')
param imageOffer string

@description('Ubuntu image SKU.')
param imageSku string

@description('Ubuntu image version.')
param imageVersion string

@description('Common tags applied to all Bastion VM resources.')
param tags object

var normalizedPrefix = toLower(namePrefix)
var bastionSubnetName = 'AzureBastionSubnet'
var workstationSubnetName = 'bastion-vm'
var vnetName = '${normalizedPrefix}-vnet'
var workstationNsgName = '${normalizedPrefix}-nsg'
var bastionPublicIpName = '${normalizedPrefix}-pip'
var natPublicIpName = '${normalizedPrefix}-nat-pip'
var natGatewayName = '${normalizedPrefix}-nat'
var bastionName = normalizedPrefix
var vmName = '${normalizedPrefix}-vm'
var nicName = '${normalizedPrefix}-nic'
var installScript = loadTextContent('scripts/install-tools.sh')
var cloudInitTemplate = loadTextContent('cloud-init.yaml')
var cloudInit = replace(cloudInitTemplate, '__INSTALL_TOOLS_B64__', base64(installScript))

resource vnet 'Microsoft.Network/virtualNetworks@2024-05-01' = {
  name: vnetName
  location: location
  tags: tags
  properties: {
    addressSpace: {
      addressPrefixes: [
        vnetAddressPrefix
      ]
    }
  }
}

resource workstationNsg 'Microsoft.Network/networkSecurityGroups@2024-05-01' = {
  name: workstationNsgName
  location: location
  tags: tags
  properties: {
    securityRules: [
      {
        name: 'AllowBastionSshInbound'
        properties: {
          priority: 100
          access: 'Allow'
          direction: 'Inbound'
          protocol: 'Tcp'
          sourceAddressPrefix: bastionSubnetPrefix
          sourcePortRange: '*'
          destinationAddressPrefix: '*'
          destinationPortRange: '22'
        }
      }
      {
        name: 'DenyVnetInbound'
        properties: {
          priority: 200
          access: 'Deny'
          direction: 'Inbound'
          protocol: '*'
          sourceAddressPrefix: 'VirtualNetwork'
          sourcePortRange: '*'
          destinationAddressPrefix: '*'
          destinationPortRange: '*'
        }
      }
    ]
  }
}

resource natPublicIp 'Microsoft.Network/publicIPAddresses@2024-05-01' = {
  name: natPublicIpName
  location: location
  sku: {
    name: 'Standard'
  }
  tags: tags
  properties: {
    publicIPAllocationMethod: 'Static'
    publicIPAddressVersion: 'IPv4'
  }
}

resource natGateway 'Microsoft.Network/natGateways@2024-05-01' = {
  name: natGatewayName
  location: location
  sku: {
    name: 'Standard'
  }
  tags: tags
  properties: {
    idleTimeoutInMinutes: 10
    publicIpAddresses: [
      {
        id: natPublicIp.id
      }
    ]
  }
}

resource bastionSubnet 'Microsoft.Network/virtualNetworks/subnets@2024-05-01' = {
  parent: vnet
  name: bastionSubnetName
  properties: {
    addressPrefix: bastionSubnetPrefix
  }
}

resource workstationSubnet 'Microsoft.Network/virtualNetworks/subnets@2024-05-01' = {
  parent: vnet
  name: workstationSubnetName
  properties: {
    addressPrefix: workstationSubnetPrefix
    networkSecurityGroup: {
      id: workstationNsg.id
    }
    natGateway: {
      id: natGateway.id
    }
  }
}

resource bastionPublicIp 'Microsoft.Network/publicIPAddresses@2024-05-01' = {
  name: bastionPublicIpName
  location: location
  sku: {
    name: 'Standard'
  }
  tags: tags
  properties: {
    publicIPAllocationMethod: 'Static'
    publicIPAddressVersion: 'IPv4'
  }
}

resource bastion 'Microsoft.Network/bastionHosts@2024-05-01' = {
  name: bastionName
  location: location
  sku: {
    name: 'Standard'
  }
  tags: tags
  properties: {
    enableTunneling: true
    ipConfigurations: [
      {
        name: 'default'
        properties: {
          subnet: {
            id: bastionSubnet.id
          }
          publicIPAddress: {
            id: bastionPublicIp.id
          }
        }
      }
    ]
  }
}

resource vmNic 'Microsoft.Network/networkInterfaces@2024-05-01' = {
  name: nicName
  location: location
  tags: tags
  properties: {
    ipConfigurations: [
      {
        name: 'ipconfig1'
        properties: {
          privateIPAllocationMethod: 'Dynamic'
          subnet: {
            id: workstationSubnet.id
          }
        }
      }
    ]
  }
}

resource vm 'Microsoft.Compute/virtualMachines@2024-07-01' = {
  name: vmName
  location: location
  tags: tags
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    hardwareProfile: {
      vmSize: vmSize
    }
    osProfile: {
      computerName: vmName
      adminUsername: adminUsername
      customData: base64(cloudInit)
      linuxConfiguration: {
        disablePasswordAuthentication: true
        provisionVMAgent: true
        patchSettings: {
          patchMode: 'ImageDefault'
          assessmentMode: 'ImageDefault'
        }
        ssh: {
          publicKeys: [
            {
              path: '/home/${adminUsername}/.ssh/authorized_keys'
              keyData: adminSshPublicKey
            }
          ]
        }
      }
    }
    storageProfile: {
      imageReference: {
        publisher: imagePublisher
        offer: imageOffer
        sku: imageSku
        version: imageVersion
      }
      osDisk: {
        name: '${normalizedPrefix}-osdisk'
        createOption: 'FromImage'
        deleteOption: 'Delete'
        managedDisk: {
          storageAccountType: osDiskStorageAccountType
        }
      }
    }
    networkProfile: {
      networkInterfaces: [
        {
          id: vmNic.id
          properties: {
            primary: true
            deleteOption: 'Delete'
          }
        }
      ]
    }
    diagnosticsProfile: {
      bootDiagnostics: {
        enabled: true
      }
    }
  }
}

resource aadSshLoginExtension 'Microsoft.Compute/virtualMachines/extensions@2024-07-01' = {
  parent: vm
  name: 'AADSSHLoginForLinux'
  location: location
  tags: tags
  properties: {
    publisher: 'Microsoft.Azure.ActiveDirectory'
    type: 'AADSSHLoginForLinux'
    typeHandlerVersion: '1.0'
    autoUpgradeMinorVersion: true
  }
}

output virtualMachineName string = vm.name
output bastionName string = bastion.name
output vnetName string = vnet.name
output workstationSubnetName string = workstationSubnet.name
