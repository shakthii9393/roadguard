"""Create (or reset the password of) an admin account.

Run from the `roadguard` project root:
    python backend/create_admin.py

There's no public sign-up endpoint on purpose - admin accounts are created
here, on the machine running the server, not over the network.
"""
import getpass
import sys

from database import Base, SessionLocal, engine
from models import User
from utils.security import hash_password

if __name__ == "__main__":
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        username = input("Admin username: ").strip()
        if not username:
            sys.exit("Username can't be empty.")

        password = getpass.getpass("Admin password: ")
        if len(password) < 8:
            sys.exit("Password must be at least 8 characters.")
        if password != getpass.getpass("Confirm password: "):
            sys.exit("Passwords didn't match.")

        existing = db.query(User).filter(User.username == username).first()
        if existing:
            existing.password_hash = hash_password(password)
            db.commit()
            print(f"Password updated for existing admin '{username}'.")
        else:
            db.add(User(username=username, password_hash=hash_password(password), role="admin"))
            db.commit()
            print(f"Admin account '{username}' created.")
    finally:
        db.close()
