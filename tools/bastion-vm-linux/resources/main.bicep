targetScope = 'resourceGroup'

metadata name = 'Data Platform Bastion VM Stack'
metadata description = 'Creates Bastion VM resources inside an existing primary platform resource group.'

@description('Azure region for all Bastion VM resources.')
param location string = resourceGroup().location

@description('Short lowercase prefix used in Bastion VM resource names. Use letters, numbers, and hyphens only.')
@minLength(3)
@maxLength(18)
param namePrefix string = 'dp-bastion'

@description('Local Linux bootstrap account name. Normal operator access uses Microsoft Entra SSH login.')
@minLength(1)
@maxLength(32)
param adminUsername string = 'azureuser'

@description('SSH public key for the required local Linux bootstrap account.')
@minLength(1)
param adminSshPublicKey string

@description('CIDR range for the Bastion VM VNet.')
param vnetAddressPrefix string = '10.253.0.0/24'

@description('CIDR range for Azure Bastion. Azure Bastion requires a dedicated subnet named AzureBastionSubnet with /26 or larger.')
param bastionSubnetPrefix string = '10.253.0.0/26'

@description('CIDR range for the Bastion VM subnet.')
param workstationSubnetPrefix string = '10.253.0.64/27'

@description('Bastion VM size.')
param vmSize string

@description('Managed OS disk SKU for the Bastion VM.')
@allowed([
  'Premium_LRS'
  'Premium_ZRS'
  'StandardSSD_LRS'
  'StandardSSD_ZRS'
])
param osDiskStorageAccountType string = 'StandardSSD_LRS'

@description('Ubuntu image publisher.')
param imagePublisher string = 'Canonical'

@description('Ubuntu image offer.')
param imageOffer string = '0001-com-ubuntu-server-jammy'

@description('Ubuntu image SKU.')
param imageSku string = '22_04-lts-gen2'

@description('Ubuntu image version.')
param imageVersion string = '22.04.202607140'

@description('Common tags applied only to Bastion VM resources.')
param tags object = {
  'data-platform-component': 'deployment-bastion-vm'
  }

module workstationResources 'resources.bicep' = {
  name: '${namePrefix}-resources'
  params: {
    adminSshPublicKey: adminSshPublicKey
    adminUsername: adminUsername
    bastionSubnetPrefix: bastionSubnetPrefix
    workstationSubnetPrefix: workstationSubnetPrefix
    imageOffer: imageOffer
    imagePublisher: imagePublisher
    imageSku: imageSku
    imageVersion: imageVersion
    location: location
    namePrefix: namePrefix
    osDiskStorageAccountType: osDiskStorageAccountType
    tags: tags
    vmSize: vmSize
    vnetAddressPrefix: vnetAddressPrefix
  }
}

output virtualMachineName string = workstationResources.outputs.virtualMachineName
output bastionName string = workstationResources.outputs.bastionName
output vnetName string = workstationResources.outputs.vnetName
output workstationSubnetName string = workstationResources.outputs.workstationSubnetName
