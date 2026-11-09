from django import forms
from django.contrib.auth import logout
from django.db import IntegrityError
from django.shortcuts import render, redirect
from django.urls import reverse
from django.views.decorators.csrf import csrf_protect
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.cache import never_cache
from rest_framework.exceptions import APIException

from collab import invitations
from users.functions.common import login


class AcceptanceForm(forms.Form):
    token = forms.CharField(max_length=128, widget=forms.HiddenInput)
    password = forms.CharField(required=False, max_length=256, widget=forms.PasswordInput)
    confirmation = forms.CharField(required=False, max_length=256, widget=forms.PasswordInput)


@never_cache
@sensitive_post_parameters('token', 'password', 'confirmation')
@csrf_protect
def accept_invitation(request):
    if request.method == 'POST' and request.POST.get('action') == 'logout':
        logout(request)
        return redirect('collab:invitation-accept')
    form = AcceptanceForm(request.POST if request.method == 'POST' else None)
    context = {'form': form, 'login_url': reverse('user-login') + '?next=' + reverse('collab:invitation-accept')}
    status = 200
    if request.method == 'POST':
        if form.is_valid():
            try:
                user, project = invitations.accept(current_user=request.user, **form.cleaned_data)
            except (APIException, IntegrityError) as exc:
                context['error'] = (str(exc.detail) if isinstance(exc, APIException)
                                    else 'Ce compte vient d’être créé. Connectez-vous avant de réessayer.')
                status = 400
            else:
                login(request, user, backend='django.contrib.auth.backends.ModelBackend')
                context['destination'] = (request.META.get('SCRIPT_NAME', '').rstrip('/')
                                          + f'/projects/{project.pk}/collaboration')
        else:
            context['error'] = 'Complétez le formulaire et ouvrez votre lien personnel d’invitation.'
            status = 400
    elif request.method != 'GET':
        status = 405
    response = render(request, 'collab/invitation.html', context, status=status)
    # A no-referrer navigation form can send Origin: null, which Django must
    # reject. Same-origin keeps the CSRF origin and discloses nothing off-site.
    response['Referrer-Policy'] = 'same-origin'
    return response


# The native DisableCSRF middleware must not exempt this anonymous credential form.
accept_invitation._dont_enforce_csrf_checks = False
