"""Rooted unknown-cost Auto-continue authority through canonical dispatch; offline only.

Real: policy activation/replay, derive_scope capture, ToolRegistry/OpenArtCLIVideo,
preflight, durable dispatch, private unpriced ledger, collection and report.
Mocked: fixture-only executable CLI, pinned HTTPS bytes, synthetic approvals and
reviews. One synthetic strict sample makes the PixVerse V6 T2V profile
fixture-qualified; nothing here claims live, production or quality qualification.
"""
import copy
import json
import os
from pathlib import Path

import pytest

from lib import openart_credit as credit, openart_dispatch as dispatch, openart_jobs as jobs
from lib import production_autonomy as autonomy, production_execution as execution
from lib import production_request as preparation
from tests.integration.test_openart_unknown_cost_workflow import workflow as unknown_workflow, submissions, read
from tests.integration.test_openart_first_pass_workflow import real_av_clip, serve_result, media_path  # noqa: F401
from tests.lib.test_production_autonomy import install_existing, make_policy
from tests.lib.test_production_request import write
from tests.lib.test_shot_contract import refresh
from tools import _openart_cli as cli

FIXTURE = 'Synthetic offline software-test approval/review; no live or semantic quality authority.'


class Governed:
    def __init__(self, tmp, monkeypatch, sample):
        self.tmp, self.mp = tmp, monkeypatch
        sample_root, sample_inputs, _, _, tool, packet = sample
        self.tool = tool
        self.clip = real_av_clip(tmp/'synthetic-source.mp4', seconds=1)
        sid = tool.execute(sample_inputs).data['production_attempt_id']
        monkeypatch.setenv('FAKE_STATUS', 'done')
        jobs.promote_result_contract(sid, json_paths={'url_hosts': ['cdn.openart.test']})
        serve_result(monkeypatch, self.clip.read_bytes())
        execution.collect_openart_attempt(sample_root, sid, request_sha256=packet['request_sha256'])
        dispatch.resolve_attempt(sample_root, sid, packet['request_sha256'])
        monkeypatch.delenv('FAKE_STATUS')
        self.profile = jobs.load_qualification(model='pixverseV6', mode='text2video', require='full')
        self.job = 0
        self.next_job()
        assert len(submissions(tmp)) == 1
        self.bootstrap = 1
        self.root = tmp/'governed'; self.root.mkdir()
        contract = read(sample_root/'artifacts/shot_contract.json')
        contract['project_id'] = self.root.name
        refresh(contract)
        write(self.root/'artifacts/shot_contract.json', contract)
        for name in ('script.json', 'scene_plan.json'):
            write(self.root/'artifacts'/name, read(sample_root/'artifacts'/name))
        write(self.root/'project.json', {'project_id': self.root.name, 'story_revision': contract['story_revision'],
            'pipeline_type': 'cinematic', 'governance': {'version': '1.0', 'mode': 'strict'}})
        self.inputs = self.fresh_inputs('oa1')

    def next_job(self):
        """Give each later original a distinct synthetic job identity."""
        binary = Path(os.environ['OPENART_CLI_PATH'])
        text = binary.read_text()
        old = 'fixture-original' if self.job == 0 else f'policy-job-{self.job}'
        self.job += 1
        binary.write_text(text.replace(old, f'policy-job-{self.job}'))

    def fresh_inputs(self, ident):
        return {'project_dir': str(self.root), 'governance': {'scope_id': 'planned', 'shot_id': 'entry'},
            'operation': 'text_to_video', 'model': 'pixverseV6', 'mode': 'text2video', 'duration': 1,
            'aspect_ratio': '16:9', 'resolution': '720p', 'output_path': str(self.root/f'{ident}.mp4'),
            'compiled_request_id': ident, 'preparation_review_id': ident + 'r'}

    def prepare(self, inputs):
        authored = preparation.compile_provider_prompt(self.root, 'entry', provider='openart_cli')
        inputs['prompt'] = authored['prompt']
        argv = cli.native_dry_run_argv(inputs['prompt'], model='pixverseV6', mode='text2video', duration=1,
                                       aspect_ratio='16:9', resolution='720p')
        observed = cli.run_readonly(argv)
        inputs.update(native_dry_run_receipt_id=observed['receipt_id'],
                      native_dry_run_receipt_sha256=observed['receipt_sha256'])
        native = jobs.prepare_native_request(execution._openart_controls(inputs), self.profile)
        shot = read(self.root/'artifacts/shot_contract.json')['shots'][0]
        timing = {'method': 'segmented_estimate', 'duration_seconds': 1, 'language': 'en', 'margin_seconds': .05,
          'rationale': FIXTURE, 'overlap_policy': 'serial', 'overlap_rationale': FIXTURE, 'segments': [],
          'action_windows': [{'source_pointer': '/shot_contract/shots/0/' + key, 'value_sha256': preparation.digest(shot[key]),
            'start_seconds': a, 'end_seconds': b, 'rationale': FIXTURE}
            for key, a, b in [('dominant_action', 0, .6), ('completed_end_state', .6, .95)]]}
        compiled = preparation.prepare_compiled_request(inputs, native, self.profile, coverage=authored['coverage'], timing=timing)
        review = {'version': '1.0', 'review_id': inputs['preparation_review_id'], 'reviewer': FIXTURE, 'status': 'pass',
          'subject_sha256': preparation.digest(compiled), 'evidence_kind': 'reviewed',
          'predicates': [{'name': p, 'status': 'pass', 'severity': 'critical', 'evidence': FIXTURE} for p in sorted(preparation.PREDICATES)]}
        write(self.root/f"artifacts/compiled_request-{inputs['compiled_request_id']}.json", compiled)
        write(self.root/f"artifacts/preparation_review-{inputs['preparation_review_id']}.json", review)
        inputs['unknown_cost_evidence_id'] = credit.refresh_unknown_cost_evidence(inputs, self.profile)['evidence_id']
        return inputs

    def activate(self, *, caps=None, checkpoint_stages=()):
        policy = make_policy(checkpoint_stages=list(checkpoint_stages),
          locked={'cast': {}, 'dialogue': {}, 'sources': [], 'story_predicates': [], 'controls': {}},
          flex={'duration_s': [], 'resolution': [], 'references': {'droppable_roles': [], 'substitutes': []}},
          providers=[{'id': 'openart_cli', 'billing': 'unknown_cost_no_ceiling',
                      'exposure_acknowledgement': 'no_enforceable_credit_ceiling',
                      'account_id_sha256': self.profile['account_id_sha256'], 'workspace': '__unobserved_workspace__',
                      'routes': [{'model': 'pixverseV6', 'mode': 'text2video'}]}])
        if caps:
            policy['caps'] = caps
        self.policy, self.sha = install_existing(self.root, policy,
            templates={'entry': execution.planned_request_template(self.inputs, project_dir=self.root)})
        assert autonomy.require_active_policy(self.root)[1] == self.sha

    def derive(self, inputs, **kw):
        return autonomy.derive_scope(self.root, inputs, provider='openart_cli', **kw)

    def paid(self):
        return len(submissions(self.tmp)) - self.bootstrap


@pytest.fixture
def governed(tmp_path, monkeypatch, media_path):
    gen = unknown_workflow.__wrapped__(tmp_path, monkeypatch)
    sample = next(gen)
    try:
        yield Governed(tmp_path, monkeypatch, sample)
    finally:
        try: next(gen)
        except StopIteration: pass


def collect(g, aid):
    request = read(g.root/'production_attempts'/aid/'request.json')
    g.mp.setenv('FAKE_STATUS', 'done')
    serve_result(g.mp, g.clip.read_bytes())
    assert execution.collect_openart_attempt(g.root, aid, request_sha256=request['request_sha256'])['status'] == 'generated'
    dispatch.resolve_attempt(g.root, aid, request['request_sha256'])
    g.mp.delenv('FAKE_STATUS')
    return request


def second(g):
    g.next_job()
    inputs = g.prepare(g.fresh_inputs('oa2'))
    g.derive(inputs)
    return inputs


def _mutate_json(path, fn):
    data = read(path); fn(data); write(path, data)


TAMPERS = {
    'request_prompt': lambda g, i, s: i.update(prompt=i['prompt'] + ' Extra unapproved beat.'),
    'request_duration': lambda g, i, s: i.update(duration=2),
    'request_model': lambda g, i, s: i.update(model='pixverseV5'),
    'native_receipt': lambda g, i, s: i.update(native_dry_run_receipt_sha256='0' * 64),
    'evidence_id': lambda g, i, s: i.update(unknown_cost_evidence_id='forged-evidence'),
    'authorization_bytes': lambda g, i, s: _mutate_json(
        g.root/f"artifacts/unknown_cost_authorization-{s['id']}.json", lambda d: d.update(count=2)),
    'authorization_account': lambda g, i, s: _mutate_json(
        g.root/f"artifacts/unknown_cost_authorization-{s['id']}.json", lambda d: d.update(account_id_sha256='f' * 64)),
    'approval_evidence_bytes': lambda g, i, s: _mutate_json(
        g.root/f"approvals/autonomy-unknown-{s['id']}.json", lambda d: d.update(note='edited after derive')),
    'caller_priced_authority': lambda g, i, s: i.update(credit_authorization_id=s['id']),
    'other_scope_authority': lambda g, i, s: i.update(unknown_cost_authorization_id='policy-other-entry-0'),
    'caller_allow_unknown': lambda g, i, s: i.update(allow_unknown_cost=True, unknown_cost_authorization_id='planned'),
}


@pytest.mark.parametrize('name', sorted(TAMPERS))
def test_tamper_after_derive_submits_nothing(governed, name):
    g = governed
    g.prepare(g.inputs); g.activate()
    scope = g.derive(g.inputs)
    inputs = copy.deepcopy(g.inputs)
    TAMPERS[name](g, inputs, scope)
    try:
        result = g.tool.execute(inputs)
    except Exception:
        result = None
    assert result is None or not result.success
    assert g.paid() == 0
    assert not (g.root/'production_attempts').exists() or not any(
        read(p).get('dispatch_status') == 'submitted_async' for p in (g.root/'production_attempts').glob('*/result.json'))


def test_policy_account_mismatch_refuses_derive_before_capture(governed):
    g = governed
    g.prepare(g.inputs)
    g.profile = {**g.profile, 'account_id_sha256': 'e' * 64}
    g.activate()
    with pytest.raises(Exception):
        g.derive(g.inputs)
    assert not list((g.root/'artifacts').glob('unknown_cost_authorization-*.json'))
    assert g.paid() == 0


def test_dispatch_collect_replay_then_next_occurrence(governed):
    g = governed
    g.prepare(g.inputs); g.activate(caps={'max_total_attempts': 2, 'max_attempts_per_shot': 2, 'max_repair_attempts': 1})
    scope = g.derive(g.inputs)
    authorization = read(g.root/f"artifacts/unknown_cost_authorization-{scope['id']}.json")
    assert authorization['approved_by'] == f'policy:{g.sha}' and authorization['count'] == 1
    assert 'user_approved' not in read(g.root/f"approvals/autonomy-unknown-{scope['id']}.json") or \
        read(g.root/f"approvals/autonomy-unknown-{scope['id']}.json")['user_approved'] is not True
    result = g.tool.execute(copy.deepcopy(g.inputs))
    aid = result.data['production_attempt_id']
    assert result.data['billing'] == 'unknown' and g.paid() == 1
    # Pending original holds a second derive for the same shot.
    with pytest.raises(Exception):
        g.derive(g.prepare(g.fresh_inputs('early')))
    request = collect(g, aid)
    row = dispatch.ledger().inspect_unpriced(aid)
    assert row['slot_state'] == 'terminal' and row['billing_state'] == 'unknown' and row['billed_amount'] == ''
    # Replaying the consumed scope never submits again.
    with pytest.raises(Exception):
        g.tool.execute(copy.deepcopy(g.inputs))
    assert g.paid() == 1
    # Historical provenance still validates against the retained policy.
    frozen_contract = read(g.root/'production_attempts'/aid/'shot_contract.json')
    autonomy.validate_policy_attempt(g.root, request, request['scope'], frozen_contract,
                                     frozen_openart=execution.load_openart_frozen(request))
    counts = autonomy.root_attempt_counts(g.root, g.policy)
    assert counts['total'] == 1
    # A same-shot next occurrence is a repair naming a retained failed review of actual output.
    g.next_job()
    nxt = g.prepare(g.fresh_inputs('oa2'))
    with pytest.raises(autonomy.AutonomyError):
        g.derive(copy.deepcopy(nxt), phase='repair', replaces_attempt_ids=[aid])
    output = execution._state(g.root, request)['output']['sha256']
    execution.record_rejection(g.root, aid, {'review_id': 'fixture-fail', 'reviewer': FIXTURE,
        'story_revision': request['story_revision'], 'subject_sha256': output, 'status': 'fail',
        'predicates': [{'name': 'completed_action', 'status': 'fail', 'severity': 'critical', 'evidence': FIXTURE}]})
    scope2 = g.derive(nxt, phase='repair', replaces_attempt_ids=[aid])
    assert scope2['id'] != scope['id'] and nxt['unknown_cost_authorization_id'] == scope2['id']
    out = g.tool.execute(copy.deepcopy(nxt))
    assert out.data['billing'] == 'unknown' and g.paid() == 2
    counts = autonomy.root_attempt_counts(g.root, g.policy)
    assert counts['total'] == 2 and counts['repair'] == 1


def test_refused_original_counts_once_and_caps_do_not_reset(governed):
    g = governed
    g.prepare(g.inputs); g.activate(caps={'max_total_attempts': 2, 'max_attempts_per_shot': 2, 'max_repair_attempts': 1})
    g.derive(g.inputs)
    g.mp.setenv('FAKE_REFUSAL', 'insufficient_credit')
    refused = g.tool.execute(copy.deepcopy(g.inputs))
    g.mp.delenv('FAKE_REFUSAL')
    assert not refused.success
    refused_submissions = g.paid()
    assert refused_submissions == 1  # fixture CLI records the single refused submit
    first = refused.data['production_attempt_id']
    assert dispatch.ledger().inspect_unpriced(first)['slot_state'] == 'closed'
    dispatch.repair_outbox(g.root, first)
    assert autonomy.root_attempt_counts(g.root, g.policy)['total'] == 1
    # Replays and repeated counting never inflate or reset the consumed occurrence.
    with pytest.raises(Exception):
        g.tool.execute(copy.deepcopy(g.inputs))
    assert autonomy.root_attempt_counts(g.root, g.policy)['total'] == 1
    g.next_job()
    nxt = g.prepare(g.fresh_inputs('oa2'))
    # A refusal is not a reviewed failure: neither a fresh first pass nor an unevidenced repair is authorized.
    with pytest.raises(autonomy.AutonomyError, match='corrective reroll'):
        g.derive(copy.deepcopy(nxt))
    with pytest.raises(Exception, match='terminal_failure_invalid|repair'):
        g.derive(copy.deepcopy(nxt), phase='repair', replaces_attempt_ids=[first])
    assert not [s for s in read(g.root/'production_scopes.json')['scopes']
                if 'derived_from_policy' in s and s['id'] != g.inputs['governance']['scope_id']]
    # Deleting retained scope state cannot reset counters: journals/private rows fail closed.
    (g.root/'production_scopes.json').rename(g.root/'scopes.bak')
    with pytest.raises(Exception):
        autonomy.root_attempt_counts(g.root, g.policy)
    with pytest.raises(Exception):
        g.derive(copy.deepcopy(nxt))
    assert g.paid() == refused_submissions


def test_private_only_reservation_counts_and_blocks(governed):
    g = governed
    g.prepare(g.inputs); g.activate(caps={'max_total_attempts': 1, 'max_attempts_per_shot': 1, 'max_repair_attempts': 0})
    g.derive(g.inputs)
    seen = []
    def crash(stage, aid):
        if stage == 'provider_acceptance':
            seen.append(aid); raise RuntimeError('synthetic uncertain original')
    dispatch._CRASH_HOOK = crash
    try:
        with pytest.raises(RuntimeError):
            g.tool.execute(copy.deepcopy(g.inputs))
    finally:
        dispatch._CRASH_HOOK = None
    aid = seen[0]
    journal = g.root/'production_attempts'/aid
    assert journal.exists()
    import shutil
    shutil.rmtree(journal)
    # Private manifest + unpriced reservation remain the authoritative origin.
    assert autonomy.root_attempt_counts(g.root, g.policy)['total'] == 1
    g.next_job()
    with pytest.raises(Exception):
        g.derive(g.prepare(g.fresh_inputs('oa2')))
    assert g.paid() == 1


def test_revocation_after_derive_blocks_dispatch(governed):
    g = governed
    g.prepare(g.inputs); g.activate()
    g.derive(g.inputs)
    log = read(g.root/'artifacts/decision_log.json')
    log['decisions'].append({'decision_id': 'fixture-revoke', 'stage': 'assets', 'category': 'approval_policy',
        'subject': autonomy.ACTIVATION_SUBJECT, 'selected': 'strict', 'user_approved': True, 'reason': FIXTURE,
        'options_considered': [{'option_id': 'strict', 'label': 'Strict', 'score': 1, 'reason': FIXTURE}]})
    write(g.root/'artifacts/decision_log.json', log)
    try:
        result = g.tool.execute(copy.deepcopy(g.inputs))
    except (autonomy.AutonomyError, ValueError) as exc:
        assert 'polic' in str(exc).lower() or 'scope' in str(exc).lower()
    else:
        assert not result.success
    assert g.paid() == 0
    with pytest.raises(autonomy.AutonomyError):
        g.derive(g.prepare(g.fresh_inputs('oa2')))


def test_checkpoint_preauth_binds_active_policy_sha(governed):
    g = governed
    g.prepare(g.inputs); g.activate(checkpoint_stages=['assets'])
    _, sha, decision = autonomy.require_active_policy(g.root)
    for basis in ({'kind': 'policy', 'policy_sha256': '0' * 64, 'decision_id': decision},
                  {'kind': 'policy', 'policy_sha256': sha, 'decision_id': 'stale-activation'},
                  {'kind': 'human', 'policy_sha256': sha, 'decision_id': decision}):
        with pytest.raises(autonomy.AutonomyError, match='active policy'):
            autonomy.validate_preauth(g.root, 'assets', basis, {})


def test_completion_report_keeps_unknown_cost_unknown_and_draft(governed):
    g = governed
    g.prepare(g.inputs); g.activate()
    g.derive(g.inputs)
    aid = g.tool.execute(copy.deepcopy(g.inputs)).data['production_attempt_id']
    collect(g, aid)
    report = read(autonomy.completion_report(g.root, g.sha))
    assert report['quality_status'] == 'draft' and report['openart_credits'] == []
    exposure = report['openart_unknown_cost']
    assert exposure['cost_status'] == 'unknown' and exposure['billing'] == 'unknown_cost_no_ceiling'
    text = json.dumps(exposure)
    assert 'cost_usd' not in exposure and 'ceiling' not in exposure and '"billed_amount"' not in text
    row = next(r for r in report['attempts'] if r['attempt_id'] == aid)
    assert row['credits']['requested_charge'] == 'unknown' and row['credits'].get('billing_state') == 'unknown'
    assert row['credits'].get('requested_charge') != 0
