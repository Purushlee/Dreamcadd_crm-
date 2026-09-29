import re

with open('templates/leads/md_dashboard.html', 'r', encoding='utf-8') as f:
    content = f.read()

dataset_section = '''
    <hr style="border:none;border-top:1px solid rgba(0,0,0,0.05);margin:20px 0;">
    <div class="section-title mb-2" style="font-size:1rem;">📂 Imported Lead Datasets</div>
    
    {% if import_batches %}
      <div class="d-flex flex-column gap-3" style="max-height: 400px; overflow-y: auto;">
      {% for batch in import_batches %}
        <div style="background:rgba(163,177,198,.08); border-radius:var(--radius-sm); padding:15px; box-shadow: 4px 4px 10px rgba(163, 177, 198, 0.4), -4px -4px 10px rgba(255, 255, 255, 0.8);">
          <div class="d-flex justify-content-between align-center mb-2">
            <div style="font-weight:700;font-size:0.9rem;"><i class="bi bi-file-earmark-spreadsheet me-1 text-primary"></i> {{ batch.file_name }}</div>
            <div style="font-size:0.75rem;color:var(--text-muted);">Imported: {{ batch.uploaded_at|date:"d M Y, h:i A" }}</div>
          </div>
          <div class="d-flex gap-3 mb-3" style="font-size:0.8rem;">
            <div><strong>Total Leads:</strong> {{ batch.total_leads_count }}</div>
            <div><strong>Assigned:</strong> <span class="text-success">{{ batch.assigned_count }}</span></div>
            <div><strong>Pending:</strong> <span class="text-warning">{{ batch.pending_count }}</span></div>
          </div>
          <div class="d-flex justify-content-between">
            <a href="{% url 'dataset_leads' batch.id %}" class="btn-neu-sm btn-neu" style="text-decoration:none;"><i class="bi bi-eye-fill"></i> View Leads</a>
            <button type="button" class="btn-neu-sm" style="background:#dc3545;color:white;border:none;padding:0.3rem 0.8rem;border-radius:var(--radius-sm);cursor:pointer;" onclick="confirmDeleteDataset({{ batch.id }}, '{{ batch.file_name|escapejs }}', {{ batch.total_leads_count }})">
              <i class="bi bi-trash-fill"></i> Delete Dataset
            </button>
          </div>
        </div>
      {% endfor %}
      </div>
    {% else %}
      <div style="text-align:center;color:var(--text-muted);padding:30px 10px;background:rgba(163,177,198,.05);border-radius:var(--radius-sm);box-shadow: inset 2px 2px 5px rgba(163,177,198,0.3), inset -2px -2px 5px rgba(255,255,255,0.7);">
        <i class="bi bi-folder2-open" style="font-size:2rem;opacity:.3;display:block;margin-bottom:10px;"></i>
        <div style="font-size:0.85rem;font-weight:600;">No Imported Datasets</div>
        <div style="font-size:0.75rem;">Upload an Excel file to create your first lead dataset.</div>
      </div>
    {% endif %}

    <form id="deleteDatasetForm" method="post" style="display:none;">
      {% csrf_token %}
    </form>
'''

target_string = '''    <div style="margin-top:12px;font-size:.72rem;color:var(--text-muted);">
      <i class="bi bi-info-circle me-1"></i>
      Required columns: <code>name</code>, <code>phone</code> &nbsp;|&nbsp; Optional: <code>email</code>, <code>education</code>, <code>college</code>, <code>course</code>
    </div>'''

content = content.replace(target_string, target_string + '\\n' + dataset_section)

js_string = '''<script>'''
js_addition = '''
function confirmDeleteDataset(batchId, fileName, leadCount) {
  if (confirm(⚠️ Delete Entire Dataset?\\n\\n\\n\\n leads will be permanently deleted.\\nThis includes leads currently assigned to callers.\\nThis action cannot be undone.)) {
    const form = document.getElementById('deleteDatasetForm');
    form.action = /md/delete-dataset//;
    form.submit();
  }
}
'''
content = content.replace(js_string, js_string + '\\n' + js_addition)

with open('templates/leads/md_dashboard.html', 'w', encoding='utf-8') as f:
    f.write(content)

print("md_dashboard.html updated")
