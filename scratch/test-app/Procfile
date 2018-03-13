web:     bash -c 'while [ 1 ]; do echo -e "webbing on $PORT with\n$(env)"; sleep 5; done'
worker:  env TEST_ENV=1 ENV_TEST=1 bash -c 'while [ 1 ]; do echo -e "working with\n$(env)..."; sleep 5; done'
release: bash -c 'echo -e "\n$(env)\n"'
