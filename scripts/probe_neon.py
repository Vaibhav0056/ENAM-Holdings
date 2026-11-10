"""Probe: can we reach Neon, which Postgres version, and are pgvector + full-text search available?"""
import sys, time
sys.path.insert(0, ".")
from sqlalchemy import create_engine, text
from finn.config import database_url, is_postgres

url = database_url()
assert is_postgres(url), "DATABASE_URL is not set to Postgres"
t = time.time()
eng = create_engine(url, pool_pre_ping=True)
with eng.begin() as c:
    print("connect+first query s:", round(time.time() - t, 2))
    print("version:", c.execute(text("select version()")).scalar().split(",")[0])
    print("database:", c.execute(text("select current_database()")).scalar())
    avail = c.execute(text("select name, default_version from pg_available_extensions where name in ('vector','pg_trgm')")).all()
    print("available extensions:", avail)
    c.execute(text("create extension if not exists vector"))
    print("vector installed:", c.execute(text("select extversion from pg_extension where extname='vector'")).scalar())
    print("tsvector:", c.execute(text("select to_tsvector('english', 'Revenue grew 12 percent') @@ plainto_tsquery('english','revenue growth')")).scalar())
    print("vector op:", c.execute(text("select '[1,0,0]'::vector <=> '[0,1,0]'::vector")).scalar())
    print("existing tables:", [r[0] for r in c.execute(text("select tablename from pg_tables where schemaname='public'"))])
t = time.time()
with eng.connect() as c:
    c.execute(text("select 1"))
print("warm query s:", round(time.time() - t, 3))
