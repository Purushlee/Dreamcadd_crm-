content = '''{% extends "leads/base.html" %}
{% block title %}Dataset Leads - DreamCadd CRM{% endblock %}
{% block nav_imports %}active{% endblock %}

{% block content %}
<div class="section-header mb-4">
  <div>
    <h1 class="page-title"><i class="bi bi-file-earmark-spreadsheet text-accent me-2"></i>Dataset Leads</h1>
    <div class="page-sub">{{ batch.file_name }} | {{ batch.uploaded_at|date:"d M Y, h:i A" }}</div>
  </div>
  <a href="{% url 'md_dashboard' %}" class="btn-neu btn-neu-sm"><i class="bi bi-arrow-left"></i> Dashboard</a>
</div>

<div class="card fade-in">
  <div class="section-title mb-3" style="font-size:.95rem;"><i class="bi bi-list-ul text-success me-1"></i>All Leads in Dataset ({{ leads|length }})</div>

  {% if leads %}
  <div class="table-responsive">
    <table class="table-neu">
      <thead>
        <tr>
          <th>Name</th>
          <th>Phone</th>
          <th>Course</th>
          <th>Status</th>
          <th>Assigned To</th>
          <th>Date Added</th>
        </tr>
      </thead>
      <tbody>
        {% for lead in leads %}
        <tr>
          <td>{{ lead.name }}</td>
          <td>{{ lead.phone }}</td>
          <td>{{ lead.interested_course.name|default:"-" }}</td>
          <td><span class="badge-pill badge-neutral">{{ lead.get_status_display }}</span></td>
          <td>
            {% if lead.assigned_to %}
              <span class="text-success">{{ lead.assigned_to.get_full_name|default:lead.assigned_to.username }}</span>
            {% else %}
              <span class="text-warning">Unassigned</span>
            {% endif %}
          </td>
          <td>{{ lead.created_at|date:"d M Y" }}</td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
  </div>
  {% else %}
  <div style="text-align:center;color:var(--text-muted);padding:30px 10px;">
    <i class="bi bi-folder-x" style="font-size:2rem;opacity:.3;display:block;margin-bottom:10px;"></i>
    <div>No leads found in this dataset.</div>
  </div>
  {% endif %}
</div>
{% endblock %}
'''

with open('templates/leads/dataset_leads.html', 'w', encoding='utf-8') as f:
    f.write(content)

print("dataset_leads.html created")
