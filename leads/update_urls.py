import re

with open('urls.py', 'r', encoding='utf-8') as f:
    urls_content = f.read()

urls_content = urls_content.replace(
    'path("md/clear-database/", views.clear_database, name="clear_database"),',
    'path("md/clear-database/", views.clear_database, name="clear_database"),\n    path("md/delete-dataset/<int:batch_id>/", views.delete_dataset, name="delete_dataset"),\n    path("md/dataset-leads/<int:batch_id>/", views.dataset_leads, name="dataset_leads"),'
)

with open('urls.py', 'w', encoding='utf-8') as f:
    f.write(urls_content)

print("urls.py updated")
