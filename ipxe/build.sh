#!/bin/bash -e

sudo zypper -n install cross-aarch64-gcc11 xz-devel

test -d ipxe || git clone https://github.com/ipxe/ipxe.git
pushd ipxe/src

touch config/local/{timer,usb,settings,fdt}.h

# this would compile the ipxe version using its internal driver implementations
#make -j3 EMBED=../../boot.ipxe bin/ipxe.pxe bin-x86_64-efi/ipxe.efi
#make -j3 EMBED=../../boot.ipxe CROSS=aarch64-suse-linux- bin-arm64-efi/ipxe.efi

# undionly / snponly uses the network driver of the PXE / UEFI stack
make -j3 EMBED=../../boot.ipxe bin/undionly.kpxe bin-x86_64-efi/snponly.efi
make -j3 EMBED=../../boot.ipxe CROSS=aarch64-suse-linux- bin-arm64-efi/snponly.efi

sudo mv -v bin/undionly.kpxe /srv/tftpboot/ipxe/ipxe.pxe
sudo mv -v bin-x86_64-efi/snponly.efi /srv/tftpboot/ipxe/ipxe.efi
sudo mv -v bin-arm64-efi/snponly.efi /srv/tftpboot/ipxe/ipxe-arm64.efi
popd
