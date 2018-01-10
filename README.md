# Cabotage Ap

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
