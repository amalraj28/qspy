import requests
from dotenv import load_dotenv
import os

load_dotenv(".env")

print(os.getenv("API_TOKEN"))



