import logging
from collections import Counter, defaultdict

import pandas as pd
from .utils import parse_attempts_json

LOGGER = logging.getLogger(__name__)


def process_part_b(data):
    LOGGER.info("Processing Part B: API Performance Analytics")
    api = data['api_usage'].copy()
    sh = data['search_history'].copy()
    results = {}

    total_requests = len(api)

    # Parse attempts_json for all rows
    LOGGER.debug("Parsing attempts_json")
    all_attempts = []
    for idx, row in api.iterrows():
        attempts = parse_attempts_json(row.get('attempts_json', '[]'))
        all_attempts.append(attempts)
    api['parsed_attempts'] = all_attempts

    # Q21: Success vs failure rate
    status_counts = api['status'].value_counts().to_dict()
    results['q21_success_rate'] = {
        'labels': list(status_counts.keys()),
        'values': list(status_counts.values()),
        'title': 'Request Success vs Failure Rate',
        'chart_type': 'doughnut'
    }

    # Q22: Execution path distribution
    path_counts = api['execution_path'].value_counts().to_dict()
    results['q22_execution_paths'] = {
        'labels': list(path_counts.keys()),
        'values': list(path_counts.values()),
        'title': 'Execution Path Distribution',
        'chart_type': 'doughnut'
    }

    # Q23: Latency stats
    latencies = api['duration_ms'].dropna()
    results['q23_latency_stats'] = {
        'avg': round(float(latencies.mean()), 1),
        'median': round(float(latencies.median()), 1),
        'p95': round(float(latencies.quantile(0.95)), 1),
        'p99': round(float(latencies.quantile(0.99)), 1),
        'min': round(float(latencies.min()), 1),
        'max': round(float(latencies.max()), 1),
        'std': round(float(latencies.std()), 1),
        'title': 'Latency Statistics (ms)',
        'chart_type': 'stats_card'
    }
    # Latency histogram
    bins = [0, 500, 1000, 2000, 3000, 4000, 5000, 7000, 10000]
    hist_vals = []
    hist_labels = []
    for i in range(len(bins)-1):
        count = int(((latencies >= bins[i]) & (latencies < bins[i+1])).sum())
        hist_vals.append(count)
        hist_labels.append(f"{bins[i]}-{bins[i+1]}")
    count = int((latencies >= bins[-1]).sum())
    hist_vals.append(count)
    hist_labels.append(f"{bins[-1]}+")
    results['q23_latency_histogram'] = {
        'labels': hist_labels,
        'values': hist_vals,
        'title': 'Latency Distribution (ms)',
        'chart_type': 'bar'
    }

    # Q24: Latency by execution path
    path_latency = {}
    for path in api['execution_path'].unique():
        path_data = api[api['execution_path'] == path]['duration_ms'].dropna()
        path_latency[path] = {
            'avg': round(float(path_data.mean()), 1),
            'median': round(float(path_data.median()), 1),
            'p95': round(float(path_data.quantile(0.95)), 1) if len(path_data) > 1 else round(float(path_data.max()), 1),
            'count': int(len(path_data))
        }
    results['q24_latency_by_path'] = {
        'data': path_latency,
        'labels': list(path_latency.keys()),
        'avg_values': [v['avg'] for v in path_latency.values()],
        'median_values': [v['median'] for v in path_latency.values()],
        'p95_values': [v['p95'] for v in path_latency.values()],
        'title': 'Latency by Execution Path',
        'chart_type': 'grouped_bar'
    }

    # Q25: Deterministic vs Semantic comparison
    det = api[api['execution_path'] == 'deterministic_filter']
    sem = api[api['execution_path'] == 'semantic']
    dsem = api[api['execution_path'] == 'direct_semantic']
    results['q25_det_vs_sem'] = {
        'comparison': {
            'deterministic_filter': {
                'count': int(len(det)),
                'avg_latency': round(float(det['duration_ms'].mean()), 1) if len(det) > 0 else 0,
                'avg_results': round(float(det['total_results'].mean()), 1) if len(det) > 0 else 0,
                'avg_tokens': round(float(det['total_tokens'].mean()), 1) if len(det) > 0 else 0,
            },
            'semantic': {
                'count': int(len(sem)),
                'avg_latency': round(float(sem['duration_ms'].mean()), 1) if len(sem) > 0 else 0,
                'avg_results': round(float(sem['total_results'].mean()), 1) if len(sem) > 0 else 0,
                'avg_tokens': round(float(sem['total_tokens'].mean()), 1) if len(sem) > 0 else 0,
            },
            'direct_semantic': {
                'count': int(len(dsem)),
                'avg_latency': round(float(dsem['duration_ms'].mean()), 1) if len(dsem) > 0 else 0,
                'avg_results': round(float(dsem['total_results'].mean()), 1) if len(dsem) > 0 else 0,
                'avg_tokens': round(float(dsem['total_tokens'].mean()), 1) if len(dsem) > 0 else 0,
            }
        },
        'title': 'Execution Path Comparison',
        'chart_type': 'comparison_table'
    }

    # Q26: Token consumption breakdown
    results['q26_token_consumption'] = {
        'input_tokens': {'total': int(api['input_tokens'].sum()), 'avg': round(float(api['input_tokens'].mean()), 1)},
        'output_tokens': {'total': int(api['output_tokens'].sum()), 'avg': round(float(api['output_tokens'].mean()), 1)},
        'thought_tokens': {'total': int(api['thought_tokens'].sum()), 'avg': round(float(api['thought_tokens'].mean()), 1)},
        'total_tokens': {'total': int(api['total_tokens'].sum()), 'avg': round(float(api['total_tokens'].mean()), 1)},
        'labels': ['Input Tokens', 'Output Tokens', 'Thought Tokens'],
        'values': [int(api['input_tokens'].sum()), int(api['output_tokens'].sum()), int(api['thought_tokens'].sum())],
        'title': 'Token Consumption Breakdown',
        'chart_type': 'doughnut'
    }

    # Q27: Token usage per execution path
    token_by_path = {}
    for path in api['execution_path'].unique():
        path_data = api[api['execution_path'] == path]
        token_by_path[path] = {
            'avg_total': round(float(path_data['total_tokens'].mean()), 1),
            'total': int(path_data['total_tokens'].sum()),
            'avg_input': round(float(path_data['input_tokens'].mean()), 1),
            'avg_output': round(float(path_data['output_tokens'].mean()), 1),
        }
    results['q27_tokens_by_path'] = {
        'data': token_by_path,
        'labels': list(token_by_path.keys()),
        'values': [v['avg_total'] for v in token_by_path.values()],
        'title': 'Average Token Usage per Execution Path',
        'chart_type': 'bar'
    }

    # Q28-33: Provider-level analytics (from attempts_json)
    provider_stats = defaultdict(lambda: {'count': 0, 'success': 0, 'failure': 0, 'fallback': 0,
                                           'total_latency': 0, 'total_tokens': 0})
    operation_stats = defaultdict(lambda: {'count': 0, 'total_latency': 0})
    model_stats = defaultdict(lambda: {'count': 0, 'total_latency': 0, 'success': 0, 'failure': 0})
    failure_reasons = Counter()
    fallback_count = 0
    total_attempts_count = 0
    requests_with_fallback = 0

    for attempts in api['parsed_attempts']:
        has_fallback = False
        for attempt in attempts:
            if not isinstance(attempt, dict):
                continue
            total_attempts_count += 1
            provider = attempt.get('provider', 'unknown')
            operation = attempt.get('operation', 'unknown')
            model = attempt.get('model', 'unknown')
            status = attempt.get('status', 'unknown')
            latency = attempt.get('duration_ms', 0)
            tokens = attempt.get('total_tokens', 0)
            reason = attempt.get('failure_reason', '')

            provider_stats[provider]['count'] += 1
            provider_stats[provider]['total_latency'] += latency
            provider_stats[provider]['total_tokens'] += tokens
            if status == 'success':
                provider_stats[provider]['success'] += 1
            elif status == 'fallback':
                provider_stats[provider]['fallback'] += 1
                has_fallback = True
            else:
                provider_stats[provider]['failure'] += 1

            operation_stats[operation]['count'] += 1
            operation_stats[operation]['total_latency'] += latency

            model_key = f"{provider} / {model}"
            model_stats[model_key]['count'] += 1
            model_stats[model_key]['total_latency'] += latency
            if status == 'success':
                model_stats[model_key]['success'] += 1
            else:
                model_stats[model_key]['failure'] += 1

            if reason:
                failure_reasons[reason] += 1

        if has_fallback:
            requests_with_fallback += 1

    # Q28: Provider usage distribution
    provider_usage = {k: v['count'] for k, v in provider_stats.items()}
    results['q28_provider_usage'] = {
        'labels': list(provider_usage.keys()),
        'values': list(provider_usage.values()),
        'title': 'Provider Usage Distribution',
        'chart_type': 'doughnut'
    }

    # Q29: Provider success/failure/fallback
    provider_reliability = {}
    for p, s in provider_stats.items():
        provider_reliability[p] = {
            'success': s['success'],
            'failure': s['failure'],
            'fallback': s['fallback'],
            'success_rate': round(s['success'] / s['count'] * 100, 1) if s['count'] > 0 else 0,
            'avg_latency': round(s['total_latency'] / s['count'], 1) if s['count'] > 0 else 0,
        }
    results['q29_provider_reliability'] = {
        'data': provider_reliability,
        'labels': list(provider_reliability.keys()),
        'success_values': [v['success'] for v in provider_reliability.values()],
        'failure_values': [v['failure'] for v in provider_reliability.values()],
        'fallback_values': [v['fallback'] for v in provider_reliability.values()],
        'title': 'Provider Reliability (Success/Failure/Fallback)',
        'chart_type': 'stacked_bar'
    }

    # Q30: Latency per operation
    op_latency = {}
    for op, s in operation_stats.items():
        op_latency[op] = {
            'count': s['count'],
            'avg_latency': round(s['total_latency'] / s['count'], 1) if s['count'] > 0 else 0,
            'total_latency': round(s['total_latency'], 1),
        }
    results['q30_latency_per_operation'] = {
        'data': op_latency,
        'labels': list(op_latency.keys()),
        'values': [v['avg_latency'] for v in op_latency.values()],
        'title': 'Average Latency per Operation (ms)',
        'chart_type': 'bar'
    }

    # Q31: Latency per provider+model
    model_latency = {}
    for m, s in model_stats.items():
        model_latency[m] = {
            'count': s['count'],
            'avg_latency': round(s['total_latency'] / s['count'], 1) if s['count'] > 0 else 0,
        }
    # Sort by count descending
    model_latency = dict(sorted(model_latency.items(), key=lambda x: x[1]['count'], reverse=True))
    results['q31_model_latency'] = {
        'data': model_latency,
        'labels': list(model_latency.keys()),
        'values': [v['avg_latency'] for v in model_latency.values()],
        'counts': [v['count'] for v in model_latency.values()],
        'title': 'Average Latency per Provider + Model (ms)',
        'chart_type': 'bar'
    }

    # Q32: Reranking fallback frequency
    rerank_total = operation_stats.get('reranking', {}).get('count', 0)
    voyage_fallbacks = provider_stats.get('voyage-2.5', {}).get('fallback', 0)
    nemotron_rescues = provider_stats.get('openrouter-nemotron', {}).get('success', 0)
    results['q32_reranking_fallback'] = {
        'total_reranking_calls': rerank_total,
        'voyage_fallbacks': voyage_fallbacks,
        'nemotron_rescues': nemotron_rescues,
        'fallback_rate': round(voyage_fallbacks / rerank_total * 100, 1) if rerank_total > 0 else 0,
        'requests_with_fallback': requests_with_fallback,
        'title': 'Reranking Fallback Analysis',
        'chart_type': 'stat'
    }

    # Q33: Failure reasons
    results['q33_failure_reasons'] = {
        'labels': [r for r in failure_reasons.keys() if r],
        'values': [v for r, v in failure_reasons.items() if r],
        'title': 'Failure/Fallback Reasons',
        'chart_type': 'bar'
    }

    # Q34: Estimated API cost
    groq_input = provider_stats.get('groq', {}).get('total_tokens', 0)
    voyage_input = provider_stats.get('voyage-2.5', {}).get('total_tokens', 0)
    estimated_cost = (groq_input * 0.05 / 1_000_000) + (voyage_input * 0.05 / 1_000_000)
    results['q34_estimated_cost'] = {
        'total_tokens_all': int(api['total_tokens'].sum()),
        'groq_tokens': int(groq_input),
        'voyage_tokens': int(voyage_input),
        'estimated_cost_usd': round(estimated_cost, 4),
        'cost_per_search': round(estimated_cost / total_requests * 1000, 4) if total_requests > 0 else 0,
        'title': 'Estimated API Cost',
        'chart_type': 'stat'
    }

    # Q35: Result count distribution
    rc = api['result_count'].dropna()
    rc_dist = Counter()
    for r in rc:
        if r == 0:
            rc_dist['0 results'] += 1
        elif r <= 5:
            rc_dist['1-5'] += 1
        elif r <= 10:
            rc_dist['6-10'] += 1
        elif r <= 20:
            rc_dist['11-20'] += 1
        else:
            rc_dist['20+'] += 1
    results['q35_result_distribution'] = {
        'labels': ['0 results', '1-5', '6-10', '11-20', '20+'],
        'values': [rc_dist.get(k, 0) for k in ['0 results', '1-5', '6-10', '11-20', '20+']],
        'avg_result_count': round(float(api['result_count'].mean()), 1),
        'avg_total_results': round(float(api['total_results'].mean()), 1),
        'title': 'Result Count Distribution',
        'chart_type': 'bar'
    }

    # Q36: Zero-result rate
    zero_results = int((api['total_results'] == 0).sum())
    results['q36_zero_result_rate'] = {
        'zero_count': zero_results,
        'total': total_requests,
        'percentage': round(zero_results / total_requests * 100, 1),
        'title': 'Zero-Result Rate',
        'chart_type': 'stat'
    }

    # Q37: Average total_results by path
    tr_by_path = {}
    for path in api['execution_path'].unique():
        path_data = api[api['execution_path'] == path]
        tr_by_path[path] = round(float(path_data['total_results'].mean()), 1)
    results['q37_results_by_path'] = {
        'labels': list(tr_by_path.keys()),
        'values': list(tr_by_path.values()),
        'title': 'Average Total Results by Execution Path',
        'chart_type': 'bar'
    }

    # Q38: Zero-result query terms
    merged = api.merge(sh[['request_id', 'query_text']], on='request_id', how='left')
    zero_q = merged[merged['total_results'] == 0]['query_text'].dropna().tolist()
    results['q38_zero_result_queries'] = {
        'queries': zero_q,
        'count': len(zero_q),
        'title': 'Zero-Result Search Terms',
        'chart_type': 'list'
    }

    # Q39: Query length vs result count correlation
    merged['query_length'] = merged['query_text'].fillna('').apply(lambda x: len(x.split()))
    length_results = merged.groupby('query_length')['total_results'].mean()
    results['q39_length_vs_results'] = {
        'labels': [str(int(l)) for l in length_results.index[:15]],
        'values': [round(float(v), 1) for v in length_results.values[:15]],
        'title': 'Query Length vs Average Results',
        'chart_type': 'line'
    }

    # Q40: Average API calls per request
    results['q40_avg_api_calls'] = {
        'avg': round(float(api['api_call_count'].mean()), 2),
        'distribution': api['api_call_count'].value_counts().sort_index().to_dict(),
        'labels': [str(k) for k in sorted(api['api_call_count'].value_counts().index)],
        'values': [int(api['api_call_count'].value_counts().sort_index()[k]) for k in sorted(api['api_call_count'].value_counts().index)],
        'title': 'API Calls per Request Distribution',
        'chart_type': 'bar'
    }

    # Q41: Requests with >3 attempts
    multi_attempt = sum(1 for attempts in api['parsed_attempts'] if len(attempts) > 3)
    results['q41_multi_attempt'] = {
        'count': multi_attempt,
        'percentage': round(multi_attempt / total_requests * 100, 1),
        'title': 'Requests with >3 Attempts (Fallbacks)',
        'chart_type': 'stat'
    }

    # Q42: Search throughput over time
    api['created_at'] = pd.to_datetime(api['created_at'])
    api['minute_bucket'] = api['created_at'].dt.floor('5min')
    throughput = api.groupby('minute_bucket').size()
    results['q42_throughput'] = {
        'labels': [str(t.time()) for t in throughput.index],
        'values': throughput.tolist(),
        'title': 'Search Throughput (requests per 5-min bucket)',
        'chart_type': 'line'
    }

    # Q43: Latency spikes
    api['minute_bucket2'] = api['created_at'].dt.floor('5min')
    latency_over_time = api.groupby('minute_bucket2')['duration_ms'].mean()
    results['q43_latency_over_time'] = {
        'labels': [str(t.time()) for t in latency_over_time.index],
        'values': [round(float(v), 1) for v in latency_over_time.values],
        'title': 'Average Latency Over Time (5-min buckets)',
        'chart_type': 'line'
    }

    # Q44: Voyage rate limiting
    voyage_events = []
    for idx, attempts in enumerate(api['parsed_attempts']):
        for attempt in attempts:
            if isinstance(attempt, dict) and attempt.get('provider') == 'voyage-2.5':
                voyage_events.append({
                    'status': attempt.get('status', ''),
                    'failure_reason': attempt.get('failure_reason', ''),
                    'duration_ms': attempt.get('duration_ms', 0),
                })
    voyage_ok = sum(1 for e in voyage_events if e['status'] == 'success')
    voyage_fail = sum(1 for e in voyage_events if e['status'] != 'success')
    results['q44_voyage_rate_limit'] = {
        'total_calls': len(voyage_events),
        'success': voyage_ok,
        'failures': voyage_fail,
        'failure_rate': round(voyage_fail / len(voyage_events) * 100, 1) if voyage_events else 0,
        'failure_reasons': dict(Counter(e['failure_reason'] for e in voyage_events if e['failure_reason'])),
        'title': 'Voyage-2.5 Rate Limiting Analysis',
        'chart_type': 'stat'
    }

    # Q45: Embedding latency distribution
    embed_latencies = []
    for attempts in api['parsed_attempts']:
        for attempt in attempts:
            if isinstance(attempt, dict) and attempt.get('operation') == 'embedding':
                embed_latencies.append(attempt.get('duration_ms', 0))
    if embed_latencies:
        results['q45_embedding_latency'] = {
            'avg': round(sum(embed_latencies) / len(embed_latencies), 1),
            'min': round(min(embed_latencies), 1),
            'max': round(max(embed_latencies), 1),
            'median': round(sorted(embed_latencies)[len(embed_latencies)//2], 1),
            'count': len(embed_latencies),
            'title': 'Embedding Model (embeddinggemma) Latency',
            'chart_type': 'stat'
        }
    else:
        results['q45_embedding_latency'] = {'title': 'Embedding Latency', 'chart_type': 'stat', 'avg': 0}

    # Q46: Query planning bottleneck
    qp_latencies = []
    total_latencies_for_qp = []
    for idx, row in api.iterrows():
        attempts = row['parsed_attempts']
        total_dur = row['duration_ms']
        for attempt in attempts:
            if isinstance(attempt, dict) and attempt.get('operation') == 'query_planning':
                qp_latencies.append(attempt.get('duration_ms', 0))
                total_latencies_for_qp.append(total_dur)
    if qp_latencies and total_latencies_for_qp:
        qp_pct = [round(qp / total * 100, 1) for qp, total in zip(qp_latencies, total_latencies_for_qp) if total > 0]
        results['q46_query_planning_bottleneck'] = {
            'avg_qp_latency': round(sum(qp_latencies) / len(qp_latencies), 1),
            'avg_pct_of_total': round(sum(qp_pct) / len(qp_pct), 1) if qp_pct else 0,
            'count': len(qp_latencies),
            'title': 'Query Planning as % of Total Latency',
            'chart_type': 'stat'
        }
    else:
        results['q46_query_planning_bottleneck'] = {'title': 'Query Planning Bottleneck', 'chart_type': 'stat', 'avg_qp_latency': 0}

    LOGGER.info("Part B complete: %d questions processed", len(results))
    return results
