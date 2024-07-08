# Cabotage App

Deployment tooling built on top of Kubernetes, Hashicorp Consul, Hashicorp
Vault, Docker, and Python to power automated rollout and management of software
and configuration changes, including

* end-to-end verified TLS
* secure management of secrets
* continuous delivery of changes

See [a two-minute video
demo](https://twitter.com/EWDurbin/status/968315460101042176) from late February
2018.

Originally developed for use within Python Software Foundation infrastructure.
See [the PyPI infrastructure pull
request](https://github.com/python/pypi-infra/pull/3) for more on design goals.


## Development workflow

The development environment is managed using [Docker](https://www.docker.com/)
and [Compose](https://docs.docker.com/compose/), and you will need these tools
installed before doing anything else. They create containers for all the
services the app needs, including any dependencies.

To get started, you will normally want to start the application containers, run
the database migrations, and create an "admin" user, which you can do with the
following shell commands:

```sh
$ make start
$ make migrate
$ make create-admin
```

After running these you should be able to visit `http://localhost:8000/` and use
the email address `ad@min.com` and password `admin` to log in.

The following commands are available for working with the application stack
during development:


### `make start`

Starts all the containers needed to run the application. This is normally the
first command you'll run, and will download and build any images you don't
already have locally. It may take a few minutes the first time it's run, but
later invocations are much faster.


### `make rebuild`

Most changes to application code will show up immediately without having to
explicitly restart anything. However, some changes may require rebuilding the
base application image. For example, changing the dependencies in the
`requirements.*` files, or changing the Docker config itself. Use this command
after making such changes to rebuild images and restart containers.


### `make stop`

Stops all running containers, but keeps any volumes containing database files,
caches, and other persistent state.


### `make destroy`

Stops all running containers and removes any volumes containing databases and
other persistent state. This essentially returns the environment to a blank
state as though you had just cloned the respository.


### `make migrate`

Runs any pending database migrations. This command requires the application
containers to already be running.


### `make create-admin`

Creates an admin user, organisation and project. The admin user's ID is
`ad@min.com` and its password is `admin`. This command requires the application
containers to already be running.


### `make routes`

Displays all the application's HTTP routes.


## Contributing

Before committing changes, you can lint and reformat your code using
[tox](https://tox.wiki/). To install this tool on your local machine or a
virtual environment:

```sh
$ pip install tox
```

You can then run `make lint` and `make reformat` to check your code.
