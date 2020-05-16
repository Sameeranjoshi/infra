#!/bin/bash

set -ex

ENV=$(curl -sf http://169.254.169.254/latest/user-data | tr A-Z a-z || true)
ENV=${ENV:-prod}
BRANCH=master
if [[ "$ENV" == "beta" ]]; then
    BRANCH=beta
fi
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ "$1" != "--updated" || "$2" != "${BRANCH}" ]]; then
    git --work-tree ${DIR} checkout ${BRANCH}
    git --work-tree ${DIR} pull
    exec bash ${BASH_SOURCE[0]} --updated ${BRANCH}
    exit 0
fi

env EXTRA_NFS_ARGS=",ro" ${DIR}/setup-common.sh

dpkg --add-architecture i386
curl -s https://dl.winehq.org/wine-builds/winehq.key | apt-key add -
apt-add-repository 'deb https://dl.winehq.org/wine-builds/ubuntu/ bionic main'
apt-get -y update
apt-get install -y \
    binutils-multiarch \
    bison \
    bzip2 \
    cgroup-tools \
    curl \
    dos2unix \
    file \
    flex \
    g++ \
    gawk \
    gcc \
    gnat \
    jq \
    libapparmor-dev \
    libc6-dev-i386 \
    libdatetime-perl \
    libelf-dev \
    libprotobuf-dev \
    libnl-route-3-dev \
    libwww-perl \
    linux-libc-dev \
    make \
    nfs-common \
    nginx \
    patch \
    pkg-config \
    protobuf-compiler \
    python-pip \
    python3 \
    python3-pip \
    s3cmd \
    subversion \
    texinfo \
    unzip \
    wget \
    winehq-stable=4.0.3~bionic \
    wine-stable=4.0.3~bionic \
    wine-stable-amd64=4.0.3~bionic \
    wine-stable-i386=4.0.3~bionic \
    xz-utils

pushd /tmp
git clone https://github.com/apmorton/firejail.git
cd firejail
git checkout 0.9.58.2-ce-patch.1
./configure --enable-apparmor
make -j$(nproc)
make install
popd

pushd /tmp
git clone --recursive --branch 2.9 https://github.com/google/nsjail.git
cd nsjail
make -j$(nproc)
cp nsjail /usr/local/bin/nsjail
popd

cp nginx/nginx.conf /etc/nginx/nginx.conf
systemctl restart nginx

cp /infra/init/compiler-explorer.service /lib/systemd/system/compiler-explorer.service
systemctl daemon-reload
systemctl enable compiler-explorer

if [[ -f /efs/compiler-explorer/libs/boost.tar.xz ]]; then
    mkdir -p /celibs
    tar xf /efs/compiler-explorer/libs/boost.tar.xz -C /celibs
fi

adduser --system --group ce

mkdir /home/ce/.conan
cp /compiler-explorer-image/init/remotes.json /home/ce/.conan
cp /compiler-explorer-image/init/settings.yml /home/ce/.conan
chown -Rf ce:ce /home/ce/.conan

sudo -u ce pip3 install conan
ln -s /home/ce/.local/bin/conan /bin/conan
sudo -u ce conan user -s
