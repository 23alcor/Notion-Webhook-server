import os
from dotenv import load_dotenv
load_dotenv()
from routes.notion import update_deadline_text, update_important_things_text
update_deadline_text()
update_important_things_text()
