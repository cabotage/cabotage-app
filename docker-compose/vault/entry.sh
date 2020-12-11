#!/bin/sh

export VAULT_ADDR=http://127.0.0.1:8200
if [ -f /vault/file/unseal ]; then
    echo "starting vault!"
    vault server -dev -dev-skip-init -dev-listen-address=$VAULT_DEV_LISTEN_ADDRESS -dev-root-token-id=$VAULT_DEV_ROOT_TOKEN_ID -config /etc/vault/config.hcl &
    echo "unsealing!"
    while true; do
        vault status 2>&1 >/dev/null
        if [ $? == 2 ]; then
            echo "we good"
            break
        fi
        echo "vault not up yet..."
        sleep .5
    done
    export UNSEAL_TOKEN=`cat /vault/file/unseal`
    vault operator unseal ${UNSEAL_TOKEN}
    wait
else
    echo "starting vault!"
    vault server -dev -dev-listen-address=$VAULT_DEV_LISTEN_ADDRESS -dev-root-token-id=$VAULT_DEV_ROOT_TOKEN_ID -config /etc/vault/config.hcl 2>&1 | tee $HOME/logfile &
    while true; do
        vault status 2>&1 >/dev/null
        if [ $? == 0 ]; then
            echo "we good"
            break
        fi
        echo "vault not up and initialized yet..."
        sleep .5
    done
    echo -n `grep 'Unseal Key: ' $HOME/logfile | awk '{print $NF}' | sed -r "s/\x1B\[([0-9]{1,2}(;[0-9]{1,2})?)?[m|K]//g"` > /vault/file/unseal
    echo "bootstrapping our transit key"
    VAULT_TOKEN=$VAULT_DEV_ROOT_TOKEN_ID vault secrets enable transit
    VAULT_TOKEN=$VAULT_DEV_ROOT_TOKEN_ID vault write transit/restore/cabotage-app backup=`cat /etc/vault/cabotage-vault-key.backup`
    echo "bootstrapping postgres stufffff"
    VAULT_TOKEN=$VAULT_DEV_ROOT_TOKEN_ID vault secrets enable database
    VAULT_TOKEN=$VAULT_DEV_ROOT_TOKEN_ID vault write database/config/cabotage plugin_name=postgresql-database-plugin allowed_roles="cabotage" connection_url="postgresql://postgres@db/cabotage_dev?sslmode=disable" verify_connection=false
    VAULT_TOKEN=$VAULT_DEV_ROOT_TOKEN_ID vault write database/roles/cabotage db_name=cabotage default_ttl="60s" max_ttl="120s" creation_statements="CREATE ROLE \"{{name}}\" WITH LOGIN PASSWORD '{{password}}' VALID UNTIL '{{expiration}}' IN ROLE cabotage;" revocation_statements="REASSIGN OWNED BY \"{{name}}\" TO cabotage" renew_statements="ALTER ROLE \"{{name}}\" VALID UNTIL '{{expiration}}';"
    wait
fi
