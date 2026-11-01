from __future__ import annotations

import pathlib

import pytest
from sqlalchemy.orm import sessionmaker

from ais import db as aisdb
from ais.importer import import_file

ROOT = pathlib.Path(__file__).resolve().parents[2]
SEED = ROOT / "examples" / "seed.yml"
COLLECTION_PATH = ROOT / "collection"


@pytest.fixture()
def engine(tmp_path):
    eng = aisdb.make_engine(f"sqlite:///{tmp_path}/test.db")
    aisdb.configure(eng)
    aisdb.init_db(eng)
    return eng


@pytest.fixture()
def session(engine):
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    with Session() as s:
        yield s
        s.commit()


@pytest.fixture()
def seeded(session):
    rev = import_file(session, SEED)
    session.commit()
    return rev
