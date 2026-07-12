from django.urls import path, include
from rest_framework.routers import DefaultRouter
from rest_framework_simplejwt.views import TokenRefreshView
from .views import (
    MyTokenObtainPairView,
    UsuarioListView,
    UsuarioRetrieveUpdateDestroyView,
    UsuarioRegisterView,
    DirectorProfileViewSet,
    EstudianteProfileViewSet,
    PasswordResetRequestView,
    PasswordResetInviteView,
    CustomPasswordResetConfirmView,
    LogOutAPIView,
    CertificadoViewSet,
    PruebaViewSet,
    PruebaEjercicioViewSet,
    SendActivationEmailView,
    ActivateAccountView,
    GroupViewSet,
    PermissionViewSet,
    EstudianteLeccionViewSet,
    GenerarPruebaView,
    GenerarPruebaGratisView,
    ProgresoEstudiantesView,
    EscuelaConDirectorView,
    SubmitPruebaView,
    MisPruebasView,
    MisPruebasDetalleView,
    MisCertificadosView,
    VerifyCertificadoView,
    MeView,
    MeStatsView,
    MeAchievementsView,
    TwoFASetupView,
    TwoFAVerifyView,
    TwoFADisableView,
    LeaderboardView,
    PushTokensView,
    PushTokenDeleteView,
    NotificacionesView,
    NotificacionMarcarLeidaView,
    NotificacionesMarcarTodasView,
    SocialLoginGoogleView,
    SocialLoginAppleView,
)

router = DefaultRouter()
router.register(r'perfil-director', DirectorProfileViewSet)
router.register(r'perfil-estudiante', EstudianteProfileViewSet)
router.register(r'certificados', CertificadoViewSet)
router.register(r'pruebas', PruebaViewSet)
router.register(r'prueba-ejercicio', PruebaEjercicioViewSet)
router.register(r'groups', GroupViewSet, basename='group')
router.register(r'permissions', PermissionViewSet, basename='permission')
router.register(r'estudiante-leccion', EstudianteLeccionViewSet, basename='estudiante_leccion')

urlpatterns = [
    path('login/', MyTokenObtainPairView.as_view(), name='login_view'),
    path('social/google/', SocialLoginGoogleView.as_view(), name='social_google'),
    path('social/apple/', SocialLoginAppleView.as_view(), name='social_apple'),
    path('token/refresh/', TokenRefreshView.as_view(), name='token_refresh_view'),
    path('logout/', LogOutAPIView.as_view(), name='logout_view'),
    path('register/', UsuarioRegisterView.as_view(), name='user_register_view'),
    path('list_user/', UsuarioListView.as_view(), name='list_users_view'),
    path('retrive_update_delete-user/<int:pk>/', UsuarioRetrieveUpdateDestroyView.as_view(), name='retrive_update_delete_users_view'),
    path('reset_password/', PasswordResetRequestView.as_view(), name='reset_password'),
    path('password-reset-invite/<int:user_id>/', PasswordResetInviteView.as_view(), name='password_reset_invite'),
    path('new_password/<uidb64>/<token>/', CustomPasswordResetConfirmView.as_view(), name='new_password'),
    path('send-activation-email/', SendActivationEmailView.as_view(), name='send_activation_email'),
    path('activate/<str:token>/', ActivateAccountView.as_view(), name='activate_account'),
    path('tests/generate/', GenerarPruebaView.as_view(), name='generate_test'),
    path('tests/generate_free/', GenerarPruebaGratisView.as_view(), name='generate_free_test'),
    path("progreso-estudiantes/<int:escuela_id>/", ProgresoEstudiantesView.as_view(), name="progreso-estudiantes"),
    path("registrar-escuela-director/", EscuelaConDirectorView.as_view(), name="registrar_escuela_director"),

    # Pruebas: submit + historial + detalle
    path("tests/<int:prueba_id>/submit/", SubmitPruebaView.as_view(), name="submit_test"),
    path("me/tests/", MisPruebasView.as_view(), name="mis_pruebas"),
    path("me/tests/<int:prueba_id>/", MisPruebasDetalleView.as_view(), name="mis_pruebas_detalle"),

    # Certificados
    path("me/certificates/", MisCertificadosView.as_view(), name="mis_certificados"),
    path("certificates/<uuid:codigo>/verify/", VerifyCertificadoView.as_view(), name="verify_certificado"),

    # Perfil + gamificación
    path("me/", MeView.as_view(), name="me_profile"),
    path("me/stats/", MeStatsView.as_view(), name="me_stats"),
    path("me/achievements/", MeAchievementsView.as_view(), name="me_achievements"),

    # 2FA (TOTP)
    path("me/2fa/setup/", TwoFASetupView.as_view(), name="me_2fa_setup"),
    path("me/2fa/verify/", TwoFAVerifyView.as_view(), name="me_2fa_verify"),
    path("me/2fa/disable/", TwoFADisableView.as_view(), name="me_2fa_disable"),

    # Ranking + notificaciones + push
    path("leaderboard/", LeaderboardView.as_view(), name="leaderboard"),
    path("me/push-token/", PushTokensView.as_view(), name="me_push_token"),
    path("me/push-token/<str:token>/", PushTokenDeleteView.as_view(), name="me_push_token_delete"),
    path("me/notifications/", NotificacionesView.as_view(), name="me_notifications"),
    path("me/notifications/<int:pk>/read/", NotificacionMarcarLeidaView.as_view(), name="me_notification_read"),
    path("me/notifications/read-all/", NotificacionesMarcarTodasView.as_view(), name="me_notifications_read_all"),
] + router.urls
