"""Edit history models (plan section 2). The journal file is the source of truth for
crash recovery; these rows mirror it for the UI. PlanItem stores every field the plan
digest covers, so the digest can be recomputed from the database at confirm time."""
import uuid

from django.conf import settings
from django.db import models

OPERATIONS = [("add", "Add genre"), ("remove", "Remove genre"), ("replace", "Replace genres"),
              ("restore", "Restore")]


class EditPlan(models.Model):
    STATUS = [("draft", "Draft"), ("confirmed", "Confirmed"), ("superseded", "Superseded")]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="edit_plans")
    created_at = models.DateTimeField(auto_now_add=True)
    operation = models.CharField(max_length=16, choices=OPERATIONS)
    genres = models.JSONField(default=list)
    album_ids = models.JSONField(default=list)
    album_labels = models.JSONField(default=list)
    source_job = models.ForeignKey("EditJob", null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name="restore_plans")
    status = models.CharField(max_length=16, choices=STATUS, default="draft")
    digest = models.CharField(max_length=64)
    warnings = models.JSONField(default=list)
    n_change = models.PositiveIntegerField(default=0)
    n_nochange = models.PositiveIntegerField(default=0)
    n_skip = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["-created_at"]


class PlanItem(models.Model):
    ACTIONS = [("change", "Change"), ("nochange", "No change"), ("skip", "Skip"),
               ("restore", "Restore"), ("nothing", "Nothing to do"), ("conflict", "Conflict")]

    plan = models.ForeignKey(EditPlan, on_delete=models.CASCADE, related_name="items")
    seq = models.PositiveIntegerField()
    album = models.ForeignKey("library.Album", null=True, blank=True, on_delete=models.SET_NULL)
    album_key = models.CharField(max_length=64, blank=True)
    album_label = models.CharField(max_length=2048, blank=True)
    root_path = models.CharField(max_length=4096)
    relpath = models.CharField(max_length=4096)
    format = models.CharField(max_length=32, null=True, blank=True)
    size = models.BigIntegerField(null=True, blank=True)
    mtime_ns = models.BigIntegerField(null=True, blank=True)
    inode = models.BigIntegerField(null=True, blank=True)
    sha_before = models.CharField(max_length=64, null=True, blank=True)
    sha_after_expected = models.CharField(max_length=64, null=True, blank=True)  # restore plans
    sha_current_seen = models.CharField(max_length=64, null=True, blank=True,
                                        help_text="Restore plans: the file's hash when the preview was built; a "
                                                  "forced restore only replaces exactly this version")
    genres_before = models.JSONField(null=True, blank=True)
    genres_after = models.JSONField(null=True, blank=True)
    components_before = models.JSONField(null=True, blank=True)
    components_after = models.JSONField(null=True, blank=True)
    style = models.CharField(max_length=16, null=True, blank=True)
    action = models.CharField(max_length=16, choices=ACTIONS)
    reason = models.CharField(max_length=64, null=True, blank=True)
    warnings = models.JSONField(default=list)
    error = models.TextField(null=True, blank=True)
    force = models.BooleanField(default=False, help_text="Restore plans: overwrite a later version "
                                                       "(it is backed up first)")
    source_item = models.ForeignKey("JobItem", null=True, blank=True, on_delete=models.SET_NULL,
                                    related_name="restore_plan_items")

    class Meta:
        ordering = ["seq"]
        constraints = [models.UniqueConstraint(fields=["plan", "seq"], name="unique_plan_seq")]


class EditJob(models.Model):
    STATUS = [(s, s) for s in ("queued", "running", "succeeded", "completed_with_errors", "rolled_back",
                               "failed_partial", "conflict", "failed", "interrupted", "aborted")]
    FINISHED = {"succeeded", "completed_with_errors", "rolled_back", "failed_partial", "conflict", "failed",
                "interrupted"}

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    plan = models.OneToOneField(EditPlan, on_delete=models.PROTECT, related_name="job")  # one job per plan
    kind = models.CharField(max_length=16, choices=[("apply", "Apply"), ("restore", "Restore")], default="apply")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="edit_jobs")
    created_at = models.DateTimeField(auto_now_add=True)
    policy = models.CharField(max_length=16, choices=[("stop_rollback", "Stop and roll back"),
                                                      ("continue", "Continue on error")], default="stop_rollback")
    status = models.CharField(max_length=24, choices=STATUS, default="queued")
    done = models.PositiveIntegerField(default=0)
    total = models.PositiveIntegerField(default=0)
    current = models.CharField(max_length=4096, blank=True)
    journal_path = models.CharField(max_length=4096, blank=True)
    worker_pid = models.IntegerField(null=True, blank=True)
    heartbeat_at = models.DateTimeField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    error = models.TextField(blank=True)
    recovery_pending = models.BooleanField(default=False, help_text="The journal is still open: recovery waits "
                                                                   "for a library root or an operator decision")
    recovery_note = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at"]
        permissions = [("apply_edits", "Can apply and restore genre edits")]

    @property
    def journal_id(self) -> str:
        return self.id.hex

    @property
    def finished(self) -> bool:
        return self.status in self.FINISHED


class JobItem(models.Model):
    job = models.ForeignKey(EditJob, on_delete=models.CASCADE, related_name="items")
    plan_item = models.OneToOneField(PlanItem, on_delete=models.PROTECT, related_name="job_item")
    state = models.CharField(max_length=24, default="planned")
    restore_state = models.CharField(max_length=24, blank=True, help_text="Outcome of a rollback, if any")
    stage = models.CharField(max_length=32, blank=True)
    sha_before = models.CharField(max_length=64, blank=True)
    sha_after = models.CharField(max_length=64, blank=True)
    backup_sha = models.CharField(max_length=64, blank=True)
    forced_backup_sha = models.CharField(max_length=64, blank=True,
                                         help_text="Restore jobs: backup of the later version a forced restore replaced")
    snapshot_before = models.JSONField(null=True, blank=True)
    genres_requested = models.JSONField(default=list)
    genres_read_back = models.JSONField(null=True, blank=True)
    verification = models.JSONField(null=True, blank=True)
    error = models.TextField(blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["plan_item__seq"]


class Backup(models.Model):
    sha256 = models.CharField(max_length=64, primary_key=True)
    size = models.BigIntegerField()
    store_relpath = models.CharField(max_length=512)
    created_at = models.DateTimeField(auto_now_add=True)
    last_verified_at = models.DateTimeField(null=True, blank=True)
