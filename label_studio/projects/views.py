"""This file and its contents are licensed under the Apache License 2.0. Please see the included NOTICE for copyright information and LICENSE for a copy of the license.
"""
import logging

from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render

logger = logging.getLogger(__name__)


@login_required
def project_list(request):
    return render(request, 'projects/list.html')


@login_required
def project_settings(request, pk, sub_path):
    role = request.user.get_organization_role()
    if role == 'labeller':
        return redirect(f'/projects/{pk}/data')
    if role == 'qa' and sub_path != 'members':
        return redirect(f'/projects/{pk}/settings/members')
    return render(request, 'projects/settings.html')
