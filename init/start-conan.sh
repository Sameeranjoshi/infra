#!/bin/bash

set -ex

CE_USER=ce
PATH=$PATH:/home/ubuntu/node/bin

cd /home/ubuntu/ceconan/conanproxy
git pull

npm i -g npm
npm i

sudo -u ce -H /home/${CE_USER}/.local/bin/gunicorn -b 0.0.0.0:9300 -w 4 -t 300 conans.server.server_launcher:app &

exec sudo -u ce -H --preserve-env=NODE_ENV -- \
    /home/ubuntu/node/bin/node \
    -- index.js \
    ${EXTRA_ARGS}
