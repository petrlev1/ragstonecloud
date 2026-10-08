#!/bin/sh
# Установка helper'а проверки дисков «Облака» (запускать от root).
#
#   sh deploy/install-cloud-disks.sh <пользователь облака> [путь к cloud-disks]
#
# Кладёт helper в /usr/local/sbin и разрешает указанному пользователю запускать
# РОВНО этот файл без пароля (sudoers NOPASSWD). Аргументы в правиле не ограничены
# намеренно: эта сборка sudo запрещает wildcard в аргументах, а безопасность даёт
# сам helper — он принадлежит root (пользователь не может его переписать) и
# принимает только вызов без аргументов и "--test /dev/<диск этой машины>".
set -eu

APP_USER="${1:?укажи пользователя, под которым работает облако (напр. cheba)}"
SRC="${2:-$(dirname "$0")/cloud-disks}"
DEST=/usr/local/sbin/cloud-disks
SUDOERS=/etc/sudoers.d/cloud-disks
TMP=$(mktemp)

if [ ! -f "$SRC" ]; then
    echo "нет файла helper'а: $SRC" >&2
    exit 1
fi

install -o root -g root -m 755 "$SRC" "$DEST"

cat >"$TMP" <<EOF
# Проверка дисков «Облака»: пользователю $APP_USER разрешён РОВНО этот helper.
# Аргументы не ограничиваем (в этой сборке sudo wildcard в аргументах запрещён),
# безопасность обеспечивает сам helper: он принадлежит root, недоступен пользователю
# на запись и принимает только вызов без аргументов и "--test /dev/<физический диск>".
$APP_USER ALL=(root) NOPASSWD: $DEST
EOF

# валидируем ДО установки: битый файл в /etc/sudoers.d ломает sudo целиком
visudo -cf "$TMP"
install -o root -g root -m 440 "$TMP" "$SUDOERS"
rm -f "$TMP"

echo "--- установлено ---"
ls -la "$DEST" "$SUDOERS"
echo
echo "Проверка от имени $APP_USER:  sudo -n $DEST | head -c 200"
