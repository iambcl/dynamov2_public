from dotenv import load_dotenv
load_dotenv()

from dynamov2.database.db_helper import db_helper

results = db_helper.get_repository_applications()
print(results)