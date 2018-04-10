# Cabotage App

Deployment tooling built on top of Kubernetes, Hashicorp Consul,
Hashicorp Vault, Docker, and Python to power automated rollout and
management of software and configuration changes, including

* end-to-end verified TLS
* secure management of secrets
* continuous delivery of changes

See [a two-minute video
demo](https://twitter.com/EWDurbin/status/968315460101042176) from
late February 2018.

Originally developed for use within Python Software Foundation
infrastructure. See [the PyPI infrastructure pull
request](https://github.com/python/pypi-infra/pull/3) for more on
design goals.

### Basics

1. `pipenv install --three --dev`
1. `pipenv shell`

### Set Environment Variables

Update *cabotage/server/config.py*, and then run:

```sh
$ export APP_SETTINGS="cabotage.server.config.DevelopmentConfig"
```

or

```sh
$ export APP_SETTINGS="cabotage.server.config.ProductionConfig"
```

### Create DB

```sh
$ python manage.py create_db
$ python manage.py db init
$ python manage.py db migrate
$ python manage.py create_admin
$ python manage.py create_data
```

### Run the Application

```sh
$ python manage.py runserver
```

Access the application at the address [http://localhost:5000/](http://localhost:5000/)

> Want to specify a different port?

> ```sh
> $ python manage.py runserver -h 0.0.0.0 -p 8080
> ```

### Testing

Without coverage:

```sh
$ python manage.py test
```

With coverage:

```sh
$ python manage.py cov
```
