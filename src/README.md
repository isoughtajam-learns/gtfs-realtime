# Critical Functionality
Here are some key workflows for admins, including fetching new transit system data in different environments.

## Local Docker
### Fetching new transit system metadata
#### 1. make sure db (and redis, if you want celery involved too) are up
```docker compose up -d db redis```

#### 2. apply migrations if you haven't already
```docker compose run --rm backend alembic upgrade head```

#### 3. fetch one system
```docker compose run --rm backend python src/fetcher.py --transit-system BART --force```

#### 4. or fetch everything in GTFS_METADATA at once
```docker compose run --rm backend python src/fetcher.py --all --force```
