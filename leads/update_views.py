import re

with open('views.py', 'r', encoding='utf-8') as f:
    views_content = f.read()

# 1. Update md_dashboard
md_dashboard_search = r'    upload_form = ExcelUploadForm\(\)\n    recent_activities = LeadActivity\.objects\.select_related\("lead", "actor"\)\.all\(\)\[:15\]'
md_dashboard_replace = '''    upload_form = ExcelUploadForm()
    recent_activities = LeadActivity.objects.select_related("lead", "actor").all()[:15]
    
    from django.db.models import Count, Q
    import_batches = ImportBatch.objects.annotate(
        total_leads_count=Count('leads', distinct=True),
        assigned_count=Count('leads', filter=Q(leads__assigned_to__isnull=False), distinct=True),
        pending_count=Count('leads', filter=Q(leads__assigned_to__isnull=True), distinct=True)
    ).order_by('-uploaded_at')'''

views_content = re.sub(md_dashboard_search, md_dashboard_replace, views_content)

context_search = r'        "recent_activities": recent_activities,\n        "calls_chart_labels":'
context_replace = '''        "recent_activities": recent_activities,
        "import_batches": import_batches,
        "calls_chart_labels":'''

views_content = re.sub(context_search, context_replace, views_content)

# 2. Add delete_dataset and dataset_leads views
delete_dataset_view = '''

@user_passes_test(is_md, login_url="login")
@require_POST
def delete_dataset(request, batch_id):
    """Permanently deletes an entire dataset (ImportBatch) and all associated leads."""
    batch = get_object_or_404(ImportBatch, id=batch_id)
    
    with transaction.atomic():
        leads = batch.leads.all()
        count = leads.count()
        leads.delete()  # This will cascade delete related activities, assignments, etc.
        batch.delete()
        
    messages.success(request, f"Dataset '{batch.file_name}' deleted successfully. {count} leads removed.")
    return redirect("md_dashboard")

@user_passes_test(is_md, login_url="login")
def dataset_leads(request, batch_id):
    batch = get_object_or_404(ImportBatch, id=batch_id)
    leads = batch.leads.select_related("assigned_to", "interested_course", "lead_source").all()
    return render(request, "leads/dataset_leads.html", {"batch": batch, "leads": leads})
'''

views_content += delete_dataset_view

with open('views.py', 'w', encoding='utf-8') as f:
    f.write(views_content)

print("views.py updated")
