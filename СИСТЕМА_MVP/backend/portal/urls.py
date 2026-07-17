from django.urls import path

from . import views


app_name = 'portal'


urlpatterns = [
    # Должен находиться раньше DEBUG/static MEDIA_URL и production media proxy.
    path('media/employee_photos/<path:path>', views.legacy_employee_photo, name='legacy_employee_photo'),
    path('company/', views.public_home, name='public_home'),
    path('company/news/', views.public_news, name='public_news'),
    path('company/news/<int:pk>/', views.public_publication_detail, name='public_publication_detail'),
    path('company/people/', views.public_people, name='public_people'),
    path('company/vacancies/', views.public_vacancies, name='public_vacancies'),
    path('company/contacts/', views.public_contacts, name='public_contacts'),
    path('company/media/publication/<int:pk>/', views.publication_cover, name='publication_cover'),
    path('company/media/image/<int:pk>/', views.publication_image, name='publication_image'),

    path('portal/login/', views.portal_login, name='login'),
    path('portal/logout/', views.portal_logout, name='logout'),
    path('portal/', views.dashboard, name='dashboard'),
    path('portal/news/', views.publications, name='publications'),
    path('portal/news/<int:pk>/', views.publication_detail, name='publication_detail'),
    path('portal/news/<int:pk>/acknowledge/', views.acknowledge_publication, name='acknowledge_publication'),
    path('portal/news/<int:pk>/react/', views.react_publication, name='react_publication'),
    path('portal/people/', views.people, name='people'),
    path('portal/people/<int:pk>/', views.employee_profile, name='employee_profile'),
    path('portal/rating/', views.rating, name='rating'),
    path('portal/polls/', views.polls, name='polls'),
    path('portal/polls/<int:pk>/', views.poll_detail, name='poll_detail'),
    path('portal/polls/<int:pk>/vote/', views.poll_vote, name='poll_vote'),
    path('portal/feedback/', views.feedback, name='feedback'),
    path('portal/feedback/new/', views.feedback_create, name='feedback_create'),
    path('portal/feedback/<int:pk>/', views.feedback_detail, name='feedback_detail'),
    path('portal/apps/', views.apps, name='apps'),
    path('portal/suggest/', views.suggestion_create, name='suggestion_create'),

    path('portal/manage/', views.manage, name='manage'),
    path('portal/manage/publications/new/', views.manage_publication_create, name='manage_publication_create'),
    path('portal/manage/publications/<int:pk>/', views.manage_publication_edit, name='manage_publication_edit'),
    path('portal/manage/publications/<int:pk>/action/', views.manage_publication_publish, name='manage_publication_publish'),
    path('portal/manage/polls/new/', views.manage_poll_create, name='manage_poll_create'),
    path('portal/manage/polls/<int:pk>/', views.manage_poll_edit, name='manage_poll_edit'),
    path('portal/manage/polls/<int:pk>/action/', views.manage_poll_action, name='manage_poll_action'),
    path('portal/manage/permissions/', views.manage_permissions, name='manage_permissions'),
    path('portal/manage/feedback/<int:pk>/', views.manage_feedback_detail, name='manage_feedback_detail'),
    path('portal/manage/suggestions/<int:pk>/action/', views.manage_suggestion_action, name='manage_suggestion_action'),

    path('portal/media/employee/<int:pk>/', views.employee_photo, name='employee_photo'),
    path('portal/media/feedback/<int:pk>/', views.feedback_photo, name='feedback_photo'),
    path('portal/media/suggestion/<int:pk>/', views.suggestion_photo, name='suggestion_photo'),
]
