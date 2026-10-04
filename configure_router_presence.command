#!/bin/zsh

set -e

keychain_account=$(id -un)

read "huawei_user?Usuario del Huawei [root]: "
huawei_user=${huawei_user:-root}
read -s "huawei_password?Contraseña del Huawei (no se mostrará): "
echo

read "asus_user?Usuario del ASUS: "
read -s "asus_password?Contraseña del ASUS (no se mostrará): "
echo

if [[ -z "$huawei_password" || -z "$asus_user" || -z "$asus_password" ]]; then
  echo "No se guardó nada: todos los campos son obligatorios."
  read "reply?Presioná Enter para cerrar."
  exit 1
fi

security add-generic-password -U -a "$keychain_account" -s security-cam-HUAWEI_ROUTER_USER -w "$huawei_user" >/dev/null
security add-generic-password -U -a "$keychain_account" -s security-cam-HUAWEI_ROUTER_PASSWORD -w "$huawei_password" >/dev/null
security add-generic-password -U -a "$keychain_account" -s security-cam-ASUS_ROUTER_USER -w "$asus_user" >/dev/null
security add-generic-password -U -a "$keychain_account" -s security-cam-ASUS_ROUTER_PASSWORD -w "$asus_password" >/dev/null

unset huawei_password asus_password
echo
echo "Credenciales guardadas en el Llavero de macOS."
echo "Avisame para reiniciar y validar el monitor de presencia."
read "reply?Presioná Enter para cerrar."
