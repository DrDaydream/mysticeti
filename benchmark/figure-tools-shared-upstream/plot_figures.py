#!/usr/bin/env python3
"""Export auditable CSV and four figures from the serial matrix archives."""
import argparse
import csv
import math
from pathlib import Path

from run_matrix import (PROTOCOLS, MODES, NODES, RATES, DURATION, HEADER_SIZE_BYTES,
                        ATTACK_START_MS, ATTACK_DURATION_MS, cases, case_key, nearby_rate, read)

COLORS = {10: '#0072B2', 20: '#D55E00', 35: '#009E73'}
MARKERS = {10: 'o', 20: 's', 35: '^'}


def export_data(root):
    rows, replicas = [], []
    for case in cases():
        directory = root / case_key(case)
        selected = directory / 'selected.json'
        row = dict(case, status='not_run', latency_ms=None, latency_contributors=None,
                   honest_replicas=None, zero_commit_replicas='', metric='', archive='',
                   controller_commit='', source_commit='', error='')
        row.update(measurement_basis=case.get('measurement_basis', ''),
                   positive_observation_replicas=None, upstream_tps=None)
        if selected.is_file():
            meta = read(selected)
            if meta['case'] != case:
                raise ValueError(f'Configuration mismatch in {selected}')
            attempt = directory / meta['attempt']
            if attempt.parent != directory or not meta['attempt'].startswith('attempt-'):
                raise ValueError(f'Invalid attempt directory in {selected}')
            row.update(status=meta['status'], archive=str(attempt), error=meta.get('error', ''))
            if meta['status'] == 'complete':
                result = read(attempt / 'measurement.json')
                if result['case'] != case or result['status'] != 'complete':
                    raise ValueError(f'Mismatched measurement: {attempt}')
                latency = result['latency_ms']
                if latency is not None and (not math.isfinite(latency) or latency < 0):
                    raise ValueError(f'Invalid latency: {attempt}')
                row.update({key: result.get(key) for key in (
                    'latency_ms', 'latency_contributors', 'honest_replicas', 'metric',
                    'controller_commit', 'source_commit')})
                for key in ('positive_observation_replicas', 'upstream_tps'):
                    row[key] = result.get(key)
                row['zero_commit_replicas'] = ','.join(map(str, result['zero_commit_replicas']))
                for replica in result['replicas']:
                    replicas.append(dict(case, **replica))
        rows.append(row)
    for name, data in [('measurements.csv', rows), ('replicas.csv', replicas)]:
        keys = list(dict.fromkeys(k for row in data for k in row))
        with (root / name).open('w', newline='', encoding='utf-8-sig') as handle:
            writer = csv.DictWriter(handle, fieldnames=keys)
            writer.writeheader()
            writer.writerows(data)
    return rows


def draw(rows, destination, yscale='linear'):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib.lines import Line2D
    plt.rcParams.update({'font.family': 'DejaVu Sans', 'font.size': 9,
                         'pdf.fonttype': 42, 'ps.fonttype': 42, 'axes.spines.top': False,
                         'axes.spines.right': False, 'figure.dpi': 130})
    destination.mkdir(exist_ok=True)
    lookup = {(r['protocol'], r['mode'], r['nodes'], r['nominal_rate_tps']): r for r in rows}
    figures = []
    for number, mode in enumerate(MODES, 1):
        fig, axes = plt.subplots(2, 3, figsize=(15.6, 9.6), sharey=True)
        for index, protocol in enumerate(PROTOCOLS):
            ax = axes.flat[index]
            metric = 'release -> final' if protocol not in ('Mysticeti', 'Mahi-Mahi') else 'shared commit'
            if protocol in ('Mysticeti', 'Mahi-Mahi') and mode in ('A0', 'A1'):
                metric = 'E2E; upstream cumulative'
            suffix = ' | alternating leaders' if protocol == 'Bullshark' and mode in ('A3', 'A5') else ''
            if mode == 'A1' and protocol in ('Orca-A', 'Orca-B', 'Bullshark', 'Mysticeti', 'Mahi-Mahi'):
                suffix = ' | process crashes'
            ax.set_title(f'{protocol}{suffix}\n{metric}; honest replica mean', fontsize=10)
            any_data = False
            for nodes in NODES:
                points = [lookup[(protocol, mode, nodes, rate)] for rate in RATES]
                xs = [p['offered_rate_tps'] / 1000 for p in points]
                ys = [p['latency_ms'] if p['status'] == 'complete' and p['latency_ms'] is not None
                      else float('nan') for p in points]
                if yscale == 'log':
                    ys = [v if v > 0 else float('nan') for v in ys]
                any_data |= any(math.isfinite(v) for v in ys)
                ax.plot(xs, ys, color=COLORS[nodes], marker=MARKERS[nodes],
                        linewidth=1.7, markersize=4.5, label=f'{nodes} nodes')
                for x, y, point in zip(xs, ys, points):
                    if point.get('positive_observation_replicas') == 0 and point['status'] == 'complete':
                        ax.annotate('0 obs', (x, .025 + NODES.index(nodes) * .035),
                                    xycoords=ax.get_xaxis_transform(), ha='center',
                                    color=COLORS[nodes], fontsize=7)
                    elif point['status'] == 'complete' and point['latency_ms'] is None:
                        # Axis coordinates deliberately keep N/A separate from 0 ms.
                        ax.annotate('N/A', (x, .025 + NODES.index(nodes) * .035),
                                    xycoords=ax.get_xaxis_transform(), ha='center',
                                    color=COLORS[nodes], fontsize=7,
                                    bbox=dict(facecolor='white', edgecolor='none', pad=.8))
                    elif math.isfinite(y) and (point['latency_contributors'] < point['honest_replicas']
                                              or point['zero_commit_replicas']):
                        ax.plot(x, y, 'o', markerfacecolor='none', markeredgecolor='#b2182b', markersize=9)
                if nodes == 35 or (mode == 'A1' and protocol in ('Orca-A', 'Orca-B', 'Bullshark', 'Mysticeti', 'Mahi-Mahi')):
                    offset = 7 + 12 * NODES.index(nodes) if mode == 'A1' and protocol in ('Orca-A', 'Orca-B', 'Bullshark', 'Mysticeti', 'Mahi-Mahi') else 7
                    for x, y, point in zip(xs, ys, points):
                        if math.isfinite(y) and point.get('positive_observation_replicas') != 0:
                            ax.annotate(f'{x:g}', (x, y), textcoords='offset points', xytext=(0, offset),
                                        ha='center', fontsize=7, color=COLORS[nodes])
            if not any_data:
                ax.text(.5, .5, 'No valid latency observations', transform=ax.transAxes,
                        ha='center', va='center', color='#777777')
            ax.set_xticks([r / 1000 for r in RATES])
            ax.set_xlim(4, 157)
            ax.set_xlabel('Configured offered rate (ktps)')
            ax.set_ylabel('E2E latency (ms)')
            ax.set_yscale(yscale)
            ax.grid(True, alpha=.2)
        legend = axes.flat[5]
        legend.axis('off')
        handles = [Line2D([0], [0], color=COLORS[n], marker=MARKERS[n], label=f'{n} nodes') for n in NODES]
        handles += [Line2D([0], [0], marker='o', markerfacecolor='none', color='#b2182b',
                           linestyle='None', label='Some honest replicas have no observations')]
        legend.legend(handles=handles, loc='upper left', frameon=False, fontsize=10)
        notes = (f'1 run/point; {DURATION} s; warmup 0s; no error bars.\n'
                 f'Orca-A/B and Bullshark header size: {HEADER_SIZE_BYTES} B.\n'
                 'N/A: no observations. Missing/failed: gaps.\n'
                 'Requested targets: 10,30,50,70,90,110,130,150 ktps.\n'
                 '35-node targets use the nearest runnable rate.\n'
                 'X: configured load of actually started clients.\n'
                 'Orca-B delays ABA only in A5.\n'
                 'E2E endpoints/populations differ across families.\n')
        if mode in ('A0', 'A1'):
            notes += ('Mysticeti/Mahi-Mahi: cumulative from process start;\n'
                      'zero-count replicas contribute arithmetic 0.\n'
                      '0 obs means no measured E2E observations.\n')
        if mode == 'A1':
            notes += ('All five protocols A1: tail crashes;\n'
                      'absent throughout; their input is excluded.\n')
        elif mode != 'A0':
            notes += ('Same A-label does not imply the same attack.\n'
                      f'Legacy: round 1 onward. Shared: {ATTACK_START_MS / 1000:g}-'
                      f'{(ATTACK_START_MS + ATTACK_DURATION_MS) / 1000:g} s\n'
                      'after each node initializes.\n'
                      'Bullshark includes alternating leader faults.\n')
        legend.text(.03, .65, notes, va='top', fontsize=8, linespacing=1.2,
                    transform=legend.transAxes)
        values = [r['latency_ms'] for r in rows if r['mode'] == mode and r['status'] == 'complete'
                  and r['latency_ms'] is not None and r['latency_ms'] > 0]
        if yscale == 'linear':
            axes.flat[0].set_ylim(0, max(values, default=1) * 1.16)
        elif values:
            axes.flat[0].set_ylim(min(values) / 1.2, max(values) * 1.5)
        completed = sum(r['mode'] == mode and r['status'] == 'complete' for r in rows)
        fig.suptitle(f'Figure {number} | {mode} | Input rate vs latency  ({completed}/120 cases complete)', fontsize=15, y=.985)
        fig.subplots_adjust(left=.065, right=.98, top=.88, bottom=.08, hspace=.43, wspace=.25)
        for extension in ('pdf', 'png'):
            path = destination / f'figure{number}_{mode}.{extension}'
            fig.savefig(path, dpi=240)
            figures.append(path)
        plt.close(fig)
    return figures


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    parser.add_argument('--yscale', choices=['linear', 'log'], default='linear')
    args = parser.parse_args()
    root = args.directory.resolve()
    if not root.is_dir():
        parser.error(f'No archive directory: {root}')
    rows = export_data(root)
    complete = sum(r['status'] == 'complete' for r in rows)
    print(f'CSV exported: {complete}/480 cases complete. Missing values stay empty, never zero.')
    if not complete:
        print('No completed data: figures were not fabricated.', flush=True)
        return
    for path in draw(rows, root / 'figures', args.yscale):
        print(path)


if __name__ == '__main__':
    main()
