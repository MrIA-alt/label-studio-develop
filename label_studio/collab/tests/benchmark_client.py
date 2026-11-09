"""Measure real session-authenticated HTTP traffic against an isolated Docker deployment.

Fixture creation is deliberately outside the measured phases. Every run creates a fresh
organisation and never deletes or changes existing application data.
"""

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import secrets
from threading import Barrier
import time
from urllib.parse import urlparse
import uuid


CONFIG = '''<View><Text name="text" value="$text"/>
<Choices name="category" toName="text"><Choice value="a"/><Choice value="b"/></Choices></View>'''
TARGET_USERS = 50
TARGET_TASKS = 100_000
PROJECT_COUNT = 2
SAMPLES_PER_PROJECT = 16


class BenchmarkFailure(Exception):
    """A safe error code: never include credentials, cookies or response bodies."""


def percentile(values, fraction):
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lo, hi = math.floor(position), math.ceil(position)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (position - lo)


def latency_summary(values):
    return {
        'p50': round(percentile(values, 0.50), 3) if values else None,
        'p95': round(percentile(values, 0.95), 3) if values else None,
        'max': round(max(values), 3) if values else None,
    }


def write_report(path, report):
    # The caller exclusively reserves this output before seeding any application data.
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    with temporary.open('x', encoding='utf-8') as stream:
        json.dump(report, stream, indent=2, sort_keys=True, allow_nan=False)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def environment_metadata(settings, connection):
    import django
    import label_studio
    import requests

    metadata = {
        'application_version': label_studio.__version__,
        'python': platform.python_version(),
        'django': django.get_version(),
        'requests': requests.__version__,
        'os': platform.system(),
        'architecture': platform.machine(),
        'logical_cpu_count': os.cpu_count(),
        'database_vendor': connection.vendor,
        'database_version': getattr(connection, 'pg_version', None),
        'deployment': {key: os.environ.get(key) for key in (
            'UWSGI_PROCESSES', 'UWSGI_MIN_PROCESSES', 'UWSGI_INITIAL_PROCESSES',
            'UWSGI_THREADS', 'COLLAB_API_THROTTLE', 'DEBUG',
        )},
        'server_throttle': getattr(settings, 'COLLAB_API_THROTTLE', '600/min'),
    }
    for name, location in (
        ('cgroup_cpu_max', '/sys/fs/cgroup/cpu.max'),
        ('cgroup_memory_max', '/sys/fs/cgroup/memory.max'),
    ):
        path = Path(location)
        metadata[name] = path.read_text(encoding='utf-8').strip() if path.is_file() else None
    source_root = Path(__file__).resolve().parents[2]
    metadata['backend_source_sha256'] = {
        name: hashlib.sha256((source_root / name).read_bytes()).hexdigest()
        for name in (
            'collab/api.py', 'collab/privacy.py', 'collab/permissions.py', 'collab/services.py',
            'tasks/api.py', 'tasks/models.py', 'tasks/serializers.py', 'data_manager/managers.py',
        ) if (source_root / name).is_file()
    }
    return metadata


def seed_fixture(run, task_count, user_count):
    from collab.models import ProjectAnnotationPolicy, TaskAssignment
    from django.db import transaction
    from organizations.models import Organization, OrganizationMember
    from projects.models import Project, ProjectMember
    from tasks.models import Task
    from users.models import User

    started = time.perf_counter()
    existing_tasks = Task.objects.count()
    required = min(3, user_count)
    password = secrets.token_urlsafe(32)
    actors = []
    projects = []
    # Only generated users, projects and their new organisation participate in these writes.
    with transaction.atomic():
        admin_email = f'benchmark-{run}-admin@example.test'
        admin = User.objects.create(username=admin_email, email=admin_email)
        admin.set_unusable_password()
        admin.save(update_fields=['password'])
        org = Organization.create_organization(created_by=admin, title=f'Benchmark {run}')
        admin.active_organization = org
        admin.save(update_fields=['active_organization'])
        users = []
        for index in range(user_count):
            email = f'benchmark-{run}-{index:04d}@example.test'
            user = User(username=email, email=email, active_organization=org, is_active=True)
            user.set_password(password)
            users.append(user)
        User.objects.bulk_create(users, batch_size=1000)
        if any(user.pk is None for user in users):
            raise BenchmarkFailure('fixture_database_must_return_bulk_created_user_ids')
        OrganizationMember.objects.bulk_create([
            OrganizationMember(organization=org, user=user, role=OrganizationMember.Role.LABELLER) for user in users
        ], batch_size=1000)
        actors = [{'id': user.pk, 'email': user.email, 'samples': {}} for user in users]
        total_assignments = 0
        global_index = 0
        for project_index in range(PROJECT_COUNT):
            count = task_count // PROJECT_COUNT + (project_index < task_count % PROJECT_COUNT)
            project = Project.objects.create(
                title=f'Benchmark {run} {project_index + 1}', organization=org, created_by=admin,
                label_config=CONFIG, maximum_annotations=required, overlap_cohort_percentage=100,
                is_published=True,
            )
            ProjectMember.objects.bulk_create([
                ProjectMember(project=project, user=user, enabled=True) for user in users
            ], batch_size=1000)
            policy = ProjectAnnotationPolicy.objects.create(
                project=project, required_annotations=required, blind_mode=True,
                assignment_mode='manual', review_mode='manual', allow_predictions=False,
            )
            project._collab_policy = policy
            for offset in range(0, count, 1000):
                batch = Task.objects.bulk_create([
                    Task(
                        project=project, inner_id=position + 1, overlap=required,
                        data={'text': f'Synthetic benchmark document {run}: {project_index + 1}/{position + 1}.'},
                    ) for position in range(offset, min(offset + 1000, count))
                ], batch_size=1000)
                if any(task.pk is None for task in batch):
                    raise BenchmarkFailure('fixture_database_must_return_bulk_created_task_ids')
                assignments = []
                for task in batch:
                    for slot in range(required):
                        actor_index = (global_index * required + slot) % user_count
                        user = users[actor_index]
                        assignments.append(TaskAssignment(task=task, project=project, user=user, assigned_by=admin))
                        samples = actors[actor_index]['samples'].setdefault(project.pk, [])
                        if len(samples) < SAMPLES_PER_PROJECT:
                            samples.append(task.pk)
                    global_index += 1
                TaskAssignment.objects.bulk_create(assignments, batch_size=1000)
                total_assignments += len(assignments)
            # The fixture has one uniform source field and no annotations or drafts.
            summary = project.summary
            summary.all_data_columns = {'text': count}
            summary.common_data_columns = ['text']
            summary.save(update_fields=['all_data_columns', 'common_data_columns'])
            projects.append({'id': project.pk, 'tasks': count})
        if any(not actor['samples'] for actor in actors):
            raise BenchmarkFailure('fixture_did_not_assign_a_task_to_every_actor')
        project_ids = [project['id'] for project in projects]
        if Task.objects.filter(project_id__in=project_ids).count() != task_count:
            raise BenchmarkFailure('fixture_source_task_count_mismatch')
        if TaskAssignment.objects.filter(project_id__in=project_ids).count() != task_count * required:
            raise BenchmarkFailure('fixture_assignment_count_mismatch')
    return {
        'organization_id': org.pk, 'projects': projects, 'users': user_count, 'tasks': task_count,
        'required_annotations': required, 'assignments': total_assignments, 'annotations': 0,
        'existing_tasks_before_seed': existing_tasks, 'source_fixture': 'orm_bulk_create_explicit_overlap',
        'seed_seconds': round(time.perf_counter() - started, 3),
    }, actors, password


def login_actor(base, actor, password, timeout):
    import requests

    session = requests.Session()
    try:
        response = session.get(base + '/user/login/', timeout=timeout)
        if response.status_code != 200:
            raise BenchmarkFailure('login_form_http_error')
        csrf = session.cookies.get('csrftoken')
        if not csrf:
            raise BenchmarkFailure('login_csrf_cookie_missing')
        response = session.post(
            base + '/user/login/',
            data={'email': actor['email'], 'password': password, 'csrfmiddlewaretoken': csrf},
            headers={'Referer': base + '/user/login/', 'X-CSRFToken': csrf}, timeout=timeout,
        )
        if response.status_code != 200:
            raise BenchmarkFailure('login_http_error')
        response = session.get(base + '/api/current-user/whoami', timeout=timeout, allow_redirects=False)
        if response.status_code != 200 or response.json().get('id') != actor['id']:
            raise BenchmarkFailure('session_identity_not_verified')
        session.headers.update({'Referer': base + '/', 'X-CSRFToken': session.cookies.get('csrftoken', '')})
        return {'session': session, 'samples': actor['samples']}
    except Exception:
        session.close()
        raise


def request_path(client, kind, index, project_ids):
    if kind == 'policy':
        return f'/api/extensions/v1/projects/{project_ids[index % len(project_ids)]}/policy'
    # Round-robin projects before moving to the next source, avoiding one repeatedly cached task.
    samples = client['samples']
    projects = list(samples)
    project = projects[index % len(projects)]
    ids = samples[project]
    task_id = ids[(index // len(projects)) % len(ids)]
    return f'/api/tasks/{task_id}/?resolve_uri=false'


def measure_phase(base, clients, kind, requests_per_user, project_ids, required, timeout, threshold):
    barrier = Barrier(len(clients), timeout=60)

    def worker(client):
        records = []
        barrier.wait()
        for index in range(requests_per_user):
            path = request_path(client, kind, index, project_ids)
            started = time.perf_counter_ns()
            status = None
            error = None
            try:
                response = client['session'].get(base + path, timeout=timeout, allow_redirects=False)
                elapsed = (time.perf_counter_ns() - started) / 1_000_000
                status = response.status_code
                if status != 200:
                    error = f'http_{status}'
                else:
                    payload = response.json()
                    valid = (
                        payload.get('required_annotations') == required and payload.get('blind_mode') is True
                        if kind == 'policy' else isinstance(payload.get('data', {}).get('text'), str)
                        and payload.get('id') == int(path.split('/')[3]) and payload.get('annotations', []) == []
                    )
                    if not valid:
                        error = 'unexpected_payload'
            except Exception as exc:
                elapsed = (time.perf_counter_ns() - started) / 1_000_000
                error = type(exc).__name__
            records.append({'milliseconds': round(elapsed, 6), 'status': status, 'error': error})
        return records

    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=len(clients)) as pool:
        records = [sample for batch in pool.map(worker, clients) for sample in batch]
    elapsed = time.perf_counter() - started
    successful = [row['milliseconds'] for row in records if row['error'] is None]
    errors = Counter(row['error'] for row in records if row['error'] is not None)
    p95 = percentile(successful, 0.95)
    return {
        'endpoint': 'GET project policy' if kind == 'policy' else 'GET assigned native task',
        'concurrent_clients': len(clients), 'requests_per_client': requests_per_user,
        'requests': len(records), 'successful': len(successful), 'errors': dict(errors),
        'http_statuses': dict(Counter(str(row['status']) for row in records)),
        'elapsed_seconds': round(elapsed, 3), 'requests_per_second': round(len(records) / elapsed, 3),
        'latency_ms': latency_summary(successful),
        'latency_including_errors_ms': latency_summary([row['milliseconds'] for row in records]),
        'threshold_met': not errors and p95 is not None and p95 < threshold,
        'samples': records,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', required=True)
    parser.add_argument('--tasks', type=int, default=TARGET_TASKS, help='Total source tasks across two projects.')
    parser.add_argument('--users', type=int, default=TARGET_USERS, help='Distinct labeller identities and concurrent clients.')
    parser.add_argument('--requests', type=int, default=20, help='Measured requests per user for each endpoint.')
    parser.add_argument('--timeout', type=float, default=45, help='HTTP timeout in seconds.')
    parser.add_argument('--threshold-ms', type=float, default=500, help='Strict p95 limit; default acceptance target 500 ms.')
    parser.add_argument('--output', type=Path, help='New JSON result file; existing files are never overwritten.')
    options = parser.parse_args(argv)
    if os.environ.get('COLLAB_ACCEPTANCE_TESTS') != '1':
        parser.error('Only run on an isolated deployment with COLLAB_ACCEPTANCE_TESTS=1.')
    target = urlparse(options.base_url)
    if target.scheme not in ('http', 'https') or not target.hostname or target.username or target.password or target.query or target.fragment:
        parser.error('--base-url must be an HTTP(S) URL without credentials, query or fragment.')
    if options.users < 1 or options.requests < 1 or not all(
        math.isfinite(value) and value > 0 for value in (options.timeout, options.threshold_ms)
    ):
        parser.error('--users, --requests, --timeout and --threshold-ms must be positive.')
    if options.tasks < max(PROJECT_COUNT, math.ceil(options.users / min(3, options.users))):
        parser.error('--tasks must fill two projects and supply at least one distinct assignment to each user.')
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'core.settings.label_studio')
    import django

    django.setup()
    from django.conf import settings
    from django.db import connection

    run = uuid.uuid4().hex
    output = options.output or Path(settings.BASE_DATA_DIR) / 'collab-benchmarks' / f'{run}.json'
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        output.open('x', encoding='utf-8').close()
    except FileExistsError:
        parser.error('--output already exists. Choose a new file to preserve previous results.')
    report = {
        'schema_version': 1, 'run': run, 'started_at_utc': datetime.now(timezone.utc).isoformat(),
        'state': 'started', 'objective': {'users': TARGET_USERS, 'tasks': TARGET_TASKS, 'p95_ms_below': 500},
        'configured_threshold_ms': options.threshold_ms, 'percentile_method': 'linear interpolation at (n-1)*p',
        'parameters': {'tasks': options.tasks, 'users': options.users, 'requests_per_user_per_endpoint': options.requests,
                       'timeout_seconds': options.timeout},
        'transport': target.scheme, 'environment': {},
        'dataset': None, 'warmup': {}, 'phases': {}, 'acceptance_goal_met': False,
        'authentication': 'distinct real Django login sessions with CSRF; no force_authenticate',
        'exclusions': ['fixture creation', 'password hashing and login', 'one warmup request per client and endpoint'],
    }
    clients = []
    stage = 'environment'
    exit_code = 2
    try:
        write_report(output, report)
        report['environment'] = environment_metadata(settings, connection)
        if connection.vendor != 'postgresql':
            raise BenchmarkFailure('benchmark_requires_the_isolated_postgresql_deployment')
        stage = 'seed'
        dataset, actors, password = seed_fixture(run, options.tasks, options.users)
        report['dataset'] = dataset
        report['state'] = 'fixture_ready'
        write_report(output, report)
        stage = 'login'
        with ThreadPoolExecutor(max_workers=options.users) as pool:
            clients = list(pool.map(lambda actor: login_actor(options.base_url.rstrip('/'), actor, password, options.timeout), actors))
        del password
        del actors
        base = options.base_url.rstrip('/')
        projects = [project['id'] for project in dataset['projects']]
        stage = 'warmup'
        for kind in ('policy', 'assigned_task'):
            warmup = measure_phase(base, clients, kind, 1, projects, dataset['required_annotations'], options.timeout, float('inf'))
            report['warmup'][kind] = {key: value for key, value in warmup.items() if key != 'samples'}
            write_report(output, report)
            if warmup['errors']:
                raise BenchmarkFailure('warmup_http_or_payload_error')
        for kind in ('policy', 'assigned_task'):
            stage = kind
            report['phases'][kind] = measure_phase(
                base, clients, kind, options.requests, projects, dataset['required_annotations'], options.timeout,
                options.threshold_ms,
            )
            write_report(output, report)
        observed = all(phase['threshold_met'] for phase in report['phases'].values())
        full_profile = options.tasks >= TARGET_TASKS and options.users >= TARGET_USERS
        report['profile'] = 'acceptance_scale' if full_profile else 'smoke'
        report['observed_threshold_met'] = observed
        report['acceptance_goal_met'] = full_profile and observed and options.threshold_ms <= 500
        report['state'] = 'completed'
        exit_code = 0 if observed else 1
    except Exception as exc:
        report['state'] = 'failed'
        report['failure'] = {'stage': stage, 'type': type(exc).__name__}
        if isinstance(exc, BenchmarkFailure):
            report['failure']['code'] = str(exc)
    finally:
        for client in clients:
            client['session'].close()
        report['finished_at_utc'] = datetime.now(timezone.utc).isoformat()
        write_report(output, report)
    print(json.dumps({
        'run': run, 'state': report['state'], 'profile': report.get('profile'),
        'acceptance_goal_met': report['acceptance_goal_met'], 'report': str(output),
        'endpoints': {name: {key: phase[key] for key in ('latency_ms', 'errors', 'threshold_met')}
                      for name, phase in report['phases'].items()},
    }, indent=2))
    return exit_code


if __name__ == '__main__':
    raise SystemExit(main())
