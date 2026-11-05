from django.urls import path

from collab import api

app_name = 'collab'

P = 'api/extensions/v1/projects/<int:pk>'

urlpatterns = [
    path('api/extensions/v1/projects', api.ProjectCreateAPI.as_view(), name='project-create'),
    path(f'{P}/policy', api.ProjectPolicyAPI.as_view(), name='policy'),
    path(f'{P}/publish', api.ProjectPublishAPI.as_view(), name='project-publish'),
    path(f'{P}/progress', api.ProgressAPI.as_view(), name='progress'),
    path(f'{P}/results', api.ResultsAPI.as_view(), name='results'),
    path(f'{P}/review-queue', api.ReviewQueueAPI.as_view(), name='review-queue'),
    path(f'{P}/webhooks', api.ProjectWebhooksAPI.as_view(), name='webhooks'),
    path(f'{P}/webhooks/<int:webhook_id>', api.ProjectWebhookDetailAPI.as_view(), name='webhook-detail'),
    path(f'{P}/assignments/bulk', api.BulkAssignmentAPI.as_view(), name='assignments-bulk'),
    path(f'{P}/assignments/distribute', api.DistributeAPI.as_view(), name='assignments-distribute'),
    path(f'{P}/assignments/summary', api.AssignmentSummaryAPI.as_view(), name='assignments-summary'),
    path('api/extensions/v1/my-assignments', api.MyAssignmentsAPI.as_view(), name='my-assignments'),
    path('api/extensions/v1/my-reviews', api.MyReviewsAPI.as_view(), name='my-reviews'),
    path('api/extensions/v1/reviews', api.ReviewAPI.as_view(), name='reviews'),
    path('api/extensions/v1/tasks/<int:pk>/review', api.TaskReviewAPI.as_view(), name='task-review'),
    path('api/extensions/v1/tasks/<int:pk>/final', api.FinalAnnotationAPI.as_view(), name='task-final'),
    path('api/extensions/v1/service-accounts', api.ServiceAccountsAPI.as_view(), name='service-accounts'),
    path(
        'api/extensions/v1/service-accounts/<int:pk>',
        api.ServiceAccountDetailAPI.as_view(),
        name='service-account-detail',
    ),
    path('api/extensions/v1/audit', api.AuditAPI.as_view(), name='audit'),
    path(
        'api/extensions/v1/assignments/<int:pk>/<str:action>/staff',
        api.AssignmentStaffActionAPI.as_view(),
        name='assignment-staff-action',
    ),
    path(
        'api/extensions/v1/assignments/<int:pk>/<str:action>',
        api.AssignmentActionAPI.as_view(),
        name='assignment-action',
    ),
]
