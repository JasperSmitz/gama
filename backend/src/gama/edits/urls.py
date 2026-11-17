from django.urls import path

from . import views

app_name = "edits"

urlpatterns = [
    path("", views.operation, name="operation"),
    path("replace/", views.replace, name="replace"),
    path("plans/new/", views.create_plan, name="create_plan"),
    path("plans/<uuid:plan_id>/", views.plan, name="plan"),
    path("plans/<uuid:plan_id>/confirm/", views.confirm, name="confirm"),
    path("plans/<uuid:plan_id>/refresh/", views.refresh, name="refresh"),
    path("jobs/<uuid:job_id>/", views.job, name="job"),
    path("jobs/<uuid:job_id>/restore/", views.restore, name="restore"),
    path("history/", views.history, name="history"),
]
