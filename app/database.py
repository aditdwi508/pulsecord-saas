import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy.pool import NullPool

raw_url = os.getenv("DATABASE_URL", "").strip()

# Ambil baris pertama saja jika terbawa baris kedua (DATABASE_URL_POOLED=...)
if "\n" in raw_url or "\r" in raw_url:
    for line in raw_url.splitlines():
        line = line.strip()
        if line:
            raw_url = line
            break

# Bersihkan prefix nama variabel, 'psql ', dan tanda kutip
if raw_url.startswith("DATABASE_URL="):
    raw_url = raw_url.split("=", 1)[1].strip()
if raw_url.startswith("psql "):
    raw_url = raw_url[5:].strip()
raw_url = raw_url.strip('"').strip("'")

if not raw_url:
    DATABASE_URL = "sqlite:////tmp/pulsecord.db" if os.getenv("VERCEL") else "sqlite:///./pulsecord.db"
else:
    DATABASE_URL = raw_url

if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

is_sqlite = DATABASE_URL.startswith("sqlite")
connect_args = {"check_same_thread": False} if is_sqlite else {}

if is_sqlite:
    engine = create_engine(DATABASE_URL, connect_args=connect_args)
else:
    engine = create_engine(DATABASE_URL, poolclass=NullPool, pool_pre_ping=True)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()