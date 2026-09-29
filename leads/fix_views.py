with open('views.py', 'r', encoding='utf-8') as f:
    content = f.read()

content = content.replace("    from django.db.models import Count, Q\n", "")

with open('views.py', 'w', encoding='utf-8') as f:
    f.write(content)
print('Done!')
