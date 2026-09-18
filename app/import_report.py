import argparse
from pathlib import Path

from app.database import Base, engine
from app.services.importer import import_report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("report", type=Path)
    args = parser.parse_args()

    Base.metadata.create_all(bind=engine)
    release = import_report(args.report)

    print(
        f"Imported release #{release.id}: "
        f"{release.title} [{release.status}]"
    )


if __name__ == "__main__":
    main()
