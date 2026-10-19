#!/usr/bin/env bash
set -Eeuo pipefail

export DEBIAN_FRONTEND=noninteractive

AZURE_CLI_VERSION="2.88.0"
GH_VERSION="2.86.0"
TERRAFORM_VERSION="1.15.0"
UV_VERSION="0.9.15"
PYTHON_VERSION="3.12.13"
PYTHON_BUILD="20260728"
POWERSHELL_VERSION="7.6.3"
GRAPH_AUTH_VERSION="2.38.1"
KUBECTL_VERSION="1.35.0"
KUBELOGIN_VERSION="0.2.19"

retry() {
  local attempt=1
  local delay=5
  until "$@"; do
    if (( attempt >= 5 )); then
      return 1
    fi
    sleep "$delay"
    attempt=$((attempt + 1))
    delay=$((delay * 2))
  done
}

wait_for_apt() {
  local locks=(
    /var/lib/dpkg/lock
    /var/lib/dpkg/lock-frontend
    /var/lib/apt/lists/lock
    /var/cache/apt/archives/lock
  )
  for _ in {1..60}; do
    local locked=0
    for lock in "${locks[@]}"; do
      if fuser "$lock" >/dev/null 2>&1; then
        locked=1
        break
      fi
    done
    if (( locked == 0 )); then
      return 0
    fi
    sleep 5
  done
  echo "Timed out waiting for apt locks." >&2
  return 1
}

download_verified() {
  local url="$1"
  local expected="$2"
  local destination="$3"
  retry curl -fL --proto '=https' --tlsv1.2 -o "$destination" "$url"
  printf '%s  %s\n' "$expected" "$destination" | sha256sum --check --status || {
    echo "SHA-256 verification failed for $url" >&2
    return 1
  }
}

install_base_packages() {
  wait_for_apt
  retry apt-get update
  wait_for_apt
  retry apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    git \
    jq \
    libicu70 \
    libssl3 \
    openssh-client \
    psmisc \
    procps \
    tar \
    unzip
}

install_azure_cli() {
  local archive=/tmp/azure-cli.deb
  download_verified \
    "https://packages.microsoft.com/repos/azure-cli/pool/main/a/azure-cli/azure-cli_${AZURE_CLI_VERSION}-1~jammy_amd64.deb" \
    "4decc8359ba3542becf2686474e3d068c2fc0b9bb9ec64cbcc8f5aa0cb7c2b61" \
    "$archive"
  wait_for_apt
  retry apt-get install -y --no-install-recommends "$archive"
  rm -f "$archive"
}

install_github_cli() {
  local archive=/tmp/gh.tar.gz
  local directory=/tmp/gh
  download_verified \
    "https://github.com/cli/cli/releases/download/v${GH_VERSION}/gh_${GH_VERSION}_linux_amd64.tar.gz" \
    "f3b08bd6a28420cc2229b0a1a687fa25f2b838d3f04b297414c1041ca68103c7" \
    "$archive"
  rm -rf "$directory"
  mkdir -p "$directory"
  tar -xzf "$archive" -C "$directory" --strip-components=1
  install -m 0755 "$directory/bin/gh" /usr/local/bin/gh
  rm -rf "$archive" "$directory"
}

install_terraform() {
  local archive=/tmp/terraform.zip
  download_verified \
    "https://releases.hashicorp.com/terraform/${TERRAFORM_VERSION}/terraform_${TERRAFORM_VERSION}_linux_amd64.zip" \
    "dcd4b31225dd960404f744315c0c3823a7deeda43bca0256a17fc762092d7e1b" \
    "$archive"
  unzip -p "$archive" terraform >/usr/local/bin/terraform
  chmod 0755 /usr/local/bin/terraform
  rm -f "$archive"
}

install_kubernetes_tools() {
  local kubectl_binary=/tmp/kubectl
  local kubelogin_archive=/tmp/kubelogin.zip
  local kubelogin_directory=/tmp/kubelogin

  download_verified \
    "https://dl.k8s.io/release/v${KUBECTL_VERSION}/bin/linux/amd64/kubectl" \
    "a2e984a18a0c063279d692533031c1eff93a262afcc0afdc517375432d060989" \
    "$kubectl_binary"
  install -m 0755 "$kubectl_binary" /usr/local/bin/kubectl
  rm -f "$kubectl_binary"

  download_verified \
    "https://github.com/Azure/kubelogin/releases/download/v${KUBELOGIN_VERSION}/kubelogin-linux-amd64.zip" \
    "ebaeff02aa899c5cae6a2b954b64fc02738185319df2570f7dc053451efa4b2f" \
    "$kubelogin_archive"
  rm -rf "$kubelogin_directory"
  mkdir -p "$kubelogin_directory"
  unzip -q "$kubelogin_archive" -d "$kubelogin_directory"
  install -m 0755 \
    "$kubelogin_directory/bin/linux_amd64/kubelogin" \
    /usr/local/bin/kubelogin
  rm -rf "$kubelogin_archive" "$kubelogin_directory"
}

install_uv() {
  local archive=/tmp/uv.tar.gz
  local directory=/tmp/uv
  download_verified \
    "https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/uv-x86_64-unknown-linux-gnu.tar.gz" \
    "2053df0089327569cddd6afea920c2285b482d9b123f5db9f658273e96ab792c" \
    "$archive"
  rm -rf "$directory"
  mkdir -p "$directory"
  tar -xzf "$archive" -C "$directory" --strip-components=1
  install -m 0755 "$directory/uv" /usr/local/bin/uv
  install -m 0755 "$directory/uvx" /usr/local/bin/uvx
  rm -rf "$archive" "$directory"
}

install_python() {
  local archive=/tmp/python.tar.gz
  local directory="/opt/python-${PYTHON_VERSION}"
  download_verified \
    "https://github.com/astral-sh/python-build-standalone/releases/download/${PYTHON_BUILD}/cpython-${PYTHON_VERSION}%2B${PYTHON_BUILD}-x86_64-unknown-linux-gnu-install_only.tar.gz" \
    "fd9d70e1e1ed3f6caccb4e2eefe570aa07589c8f86ddf0e87f68a96cd14272e1" \
    "$archive"
  rm -rf "$directory"
  mkdir -p "$directory"
  tar -xzf "$archive" -C "$directory" --strip-components=1
  ln -sfn "$directory/bin/python3" /usr/local/bin/python3.12
  rm -f "$archive"
}

install_powershell() {
  local archive=/tmp/powershell.tar.gz
  local directory="/opt/microsoft/powershell/${POWERSHELL_VERSION}"
  download_verified \
    "https://github.com/PowerShell/PowerShell/releases/download/v${POWERSHELL_VERSION}/powershell-${POWERSHELL_VERSION}-linux-x64.tar.gz" \
    "856d0765d2332377f9d7a4aea76efdfde4de51446e7738dde2dfda41dba9e2a7" \
    "$archive"
  rm -rf "$directory"
  mkdir -p "$directory"
  tar -xzf "$archive" -C "$directory"
  chmod 0755 "$directory/pwsh"
  ln -sfn "$directory/pwsh" /usr/local/bin/pwsh
  rm -f "$archive"
}

install_graph_module() {
  local archive=/tmp/microsoft-graph-authentication.nupkg
  local directory="/usr/local/share/powershell/Modules/Microsoft.Graph.Authentication/${GRAPH_AUTH_VERSION}"
  download_verified \
    "https://www.powershellgallery.com/api/v2/package/Microsoft.Graph.Authentication/${GRAPH_AUTH_VERSION}" \
    "7fdd28ccb79b827bc7ff10ad65ddd7c4fd088e1fe7afe445114e5c6857f68a23" \
    "$archive"
  rm -rf "$directory"
  mkdir -p "$directory"
  unzip -q "$archive" -d "$directory"
  rm -f "$archive"
  pwsh -NoProfile -Command \
    "Import-Module Microsoft.Graph.Authentication -RequiredVersion ${GRAPH_AUTH_VERSION} -ErrorAction Stop"
}

write_completion_marker() {
  mkdir -p /opt/data-platform-bastion-vm
  {
    echo "installed_at_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "az=$(az version -o json | jq -r '."azure-cli"')"
    echo "gh=$(gh --version | head -n 1)"
    echo "terraform=$(terraform version -json | jq -r '.terraform_version')"
    echo "kubectl=$(kubectl version --client --output=json | jq -r '.clientVersion.gitVersion')"
    echo "kubelogin=$(kubelogin --version | tr '\n' ' ')"
    echo "uv=$(uv --version)"
    echo "python=$(/usr/local/bin/python3.12 --version)"
    echo "powershell=$(pwsh --version)"
    echo "graph_authentication=${GRAPH_AUTH_VERSION}"
    echo "git=$(git --version)"
  } >/opt/data-platform-bastion-vm/tool-versions.env
  touch /opt/data-platform-bastion-vm/ready
}

main() {
  install_base_packages
  install_azure_cli
  install_github_cli
  install_terraform
  install_kubernetes_tools
  install_uv
  install_python
  install_powershell
  install_graph_module
  write_completion_marker
}

main "$@"
