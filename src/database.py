from sqlalchemy import create_engine

engine = create_engine("postgresql+psycopg2://@localhost/gtfs").execution_options(render_nulls=True)
