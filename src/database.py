from sqlalchemy import create_engine

from src.settings import get_settings

settings = get_settings()
engine = create_engine(settings.database_url).execution_options(render_nulls=True)
