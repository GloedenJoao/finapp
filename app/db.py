from sqlmodel import SQLModel, create_engine

engine = create_engine("sqlite:///./finapp.db",
                       connect_args={"check_same_thread":False})

def init_db():
    SQLModel.metadata.create_all(engine)