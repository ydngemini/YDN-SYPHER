/*---------------------------------------------------------------------------------------------
 *  Copyright (c) Microsoft Corporation. All rights reserved.
 *  Licensed under the MIT License. See License.txt in the project root for license information.
 *--------------------------------------------------------------------------------------------*/

import { URI } from '../../../../base/common/uri.js';
import { ILogService } from '../../../../platform/log/common/log.js';
import { INativeHostService } from '../../../../platform/native/common/native.js';
import { IWorkspaceContextService } from '../../../../platform/workspace/common/workspace.js';
import { IWorkbenchContribution, WorkbenchPhase, registerWorkbenchContribution2 } from '../../../common/contributions.js';

const PROBE_SCRIPT = '.sypher/skills/system_probe.py';
const VENV_PYTHON = '.sypher_env/bin/python';

export class SypherProbeContribution implements IWorkbenchContribution {

	static readonly ID = 'workbench.contrib.sypherProbe';

	constructor(
		@IWorkspaceContextService private readonly workspaceContextService: IWorkspaceContextService,
		@INativeHostService private readonly nativeHostService: INativeHostService,
		@ILogService private readonly logService: ILogService,
	) {
		this.runHardwareProbe();
	}

	private async runHardwareProbe(): Promise<void> {
		const folders = this.workspaceContextService.getWorkspace().folders;
		if (folders.length === 0) {
			return;
		}

		const root = folders[0].uri;
		const scriptPath = URI.joinPath(root, ...PROBE_SCRIPT.split('/')).fsPath;

		const venvPython = URI.joinPath(root, ...VENV_PYTHON.split('/')).fsPath;

		try {
			// Try venv Python first; if it doesn't exist the exec will throw and we fall back
			let raw: string;
			try {
				raw = await this.nativeHostService.sypherExecSkill(venvPython, scriptPath, '[]');
			} catch {
				this.logService.info('[SypherProbe] venv Python not found — falling back to python3');
				raw = await this.nativeHostService.sypherExecSkill('python3', scriptPath, '[]');
			}

			const report = JSON.parse(raw);

			// Print directly to the VS Code Developer Console
			console.group('%c⚡ SYPHER DMI — Hardware Report', 'color:#00ff88;font-weight:bold;font-size:12px');
			console.log('CPU %:', report.cpu_percent ?? 'n/a');
			console.log('RAM total:', report.ram_total_gb ?? '?', 'GB  |  avail:', report.ram_avail_gb ?? '?', 'GB  |  used:', report.ram_percent ?? '?', '%');
			if (Array.isArray(report.gpus) && report.gpus.length > 0) {
				for (const gpu of report.gpus) {
					console.log(`GPU [${gpu.name}]  ${gpu.temp_c}°C  util:${gpu.util_pct}%  VRAM:${gpu.vram_used_mb}/${gpu.vram_total_mb} MB`);
				}
			} else {
				console.log('GPU:', report.note ?? report.error ?? 'none detected');
			}
			console.groupEnd();

			this.logService.info(`[SypherProbe] Hardware report: ${raw}`);
		} catch (err) {
			this.logService.warn(`[SypherProbe] Hardware probe failed — is system_probe.py present and python3 installed? Error: ${err}`);
		}
	}
}

registerWorkbenchContribution2(
	SypherProbeContribution.ID,
	SypherProbeContribution,
	WorkbenchPhase.AfterRestored,
);
