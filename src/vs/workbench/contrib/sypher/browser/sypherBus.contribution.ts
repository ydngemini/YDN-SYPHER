/*---------------------------------------------------------------------------------------------
 *  Copyright (c) Microsoft Corporation. All rights reserved.
 *  Licensed under the MIT License. See License.txt in the project root for license information.
 *--------------------------------------------------------------------------------------------*/

import './media/sypher-core.css';

import { VSBuffer } from '../../../../base/common/buffer.js';
import { Disposable } from '../../../../base/common/lifecycle.js';
import { URI } from '../../../../base/common/uri.js';
import { localize } from '../../../../nls.js';
import { Action2, registerAction2 } from '../../../../platform/actions/common/actions.js';
import { ICommandService } from '../../../../platform/commands/common/commands.js';
import { IFileService } from '../../../../platform/files/common/files.js';
import { ServicesAccessor } from '../../../../platform/instantiation/common/instantiation.js';
import { InstantiationType, registerSingleton } from '../../../../platform/instantiation/common/extensions.js';
import { KeybindingWeight } from '../../../../platform/keybinding/common/keybindingsRegistry.js';
import { ILogService } from '../../../../platform/log/common/log.js';
import { INotificationService, Severity } from '../../../../platform/notification/common/notification.js';
import { IWorkspaceContextService } from '../../../../platform/workspace/common/workspace.js';
import { KeyCode, KeyMod } from '../../../../base/common/keyCodes.js';
import { IWorkbenchContribution, WorkbenchPhase, registerWorkbenchContribution2 } from '../../../common/contributions.js';
import {
	ISypherAgentService,
	LoadSypherAgentAction,
	SypherAgentLoader,
	SypherAgentService,
} from './sypherAgentLoader.js';
import {
	ISypherHudService,
	SypherHudService,
	SypherTask,
	SypherTaskStatus,
} from './sypherForgePreview.js';

const SYPHER_DIR = '.sypher';
const SKILLS_DIR = 'skills';
const UI_EVENTS_FILE = '.sypher/ui_events.jsonl';

const SYSTEM_PROBE_PY = `#!/usr/bin/env python3
"""system_probe.py - SYPHER skill. Outputs minified JSON hardware report."""

import json
import subprocess
import sys


def cpu_ram():
    try:
        import psutil
        vm = psutil.virtual_memory()
        return {
            "cpu_percent": psutil.cpu_percent(interval=0.1),
            "ram_total_gb": round(vm.total / 1024 ** 3, 2),
            "ram_avail_gb": round(vm.available / 1024 ** 3, 2),
            "ram_percent": vm.percent,
        }
    except ImportError:
        return {"error": "psutil not installed - pip install psutil"}


def gpu():
    try:
        r = subprocess.run(
            ["nvidia-smi",
             "--query-gpu=name,temperature.gpu,utilization.gpu,memory.used,memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=4,
        )
        if r.returncode != 0:
            return {"gpus": [], "error": r.stderr.strip()}
        gpus = []
        for line in r.stdout.strip().splitlines():
            n, t, u, mu, mt = [v.strip() for v in line.split(",")]
            gpus.append({"name": n, "temp_c": int(t), "util_pct": int(u),
                         "vram_used_mb": int(mu), "vram_total_mb": int(mt)})
        return {"gpus": gpus}
    except FileNotFoundError:
        return {"gpus": [], "note": "nvidia-smi not found"}
    except Exception as e:
        return {"gpus": [], "error": str(e)}


if __name__ == "__main__":
    print(json.dumps({**cpu_ram(), **gpu()}, separators=(",", ":")))
`;


export class SypherBusContribution extends Disposable implements IWorkbenchContribution {

	static readonly ID = 'workbench.contrib.sypherBus';

	private _uiEventsOffset = 0;

	constructor(
		@IWorkspaceContextService private readonly workspaceContextService: IWorkspaceContextService,
		@IFileService private readonly fileService: IFileService,
		@ILogService private readonly logService: ILogService,
		@INotificationService private readonly notificationService: INotificationService,
		@ICommandService private readonly commandService: ICommandService,
		@ISypherHudService private readonly hudService: ISypherHudService,
	) {
		super();
		this.activateHud();
		this.initializeSypherDirectory();
		this.watchUiEvents();
	}

	/** Fire the boot animation on the root workbench node. */
	private activateHud(): void {
		document.querySelector<HTMLElement>('.monaco-workbench')?.classList.add('sypher-active');
	}

	// -- .sypher/skills/ scaffolding -------------------------------------------

	private async initializeSypherDirectory(): Promise<void> {
		const folders = this.workspaceContextService.getWorkspace().folders;
		if (folders.length === 0) {
			return;
		}

		const workspaceRoot = folders[0].uri;
		const skillsDir = URI.joinPath(workspaceRoot, SYPHER_DIR, SKILLS_DIR);

		try {
			if (!await this.fileService.exists(skillsDir)) {
				await this.scaffoldSkillsDirectory(workspaceRoot, skillsDir);
			} else {
				this.logService.trace(`[SypherBus] .sypher/skills/ already exists`);
			}
		} catch (err) {
			this.logService.error(`[SypherBus] Failed to initialize .sypher directory: ${err}`);
		}
	}

	private async scaffoldSkillsDirectory(workspaceRoot: URI, skillsDir: URI): Promise<void> {
		this.logService.info(`[SypherBus] Scaffolding .sypher/skills/ at ${workspaceRoot.fsPath}`);
		await this.fileService.createFolder(skillsDir);

		const probeFile = URI.joinPath(skillsDir, 'system_probe.py');
		await this.fileService.createFile(probeFile, VSBuffer.fromString(SYSTEM_PROBE_PY));

		this.notificationService.notify({
			severity: Severity.Info,
			message: localize('sypher.scaffolded', "YDN SYPHER: Initialized .sypher/skills/ with system_probe.py"),
		});
	}

	// -- UI events watcher -----------------------------------------------------

	private async watchUiEvents(): Promise<void> {
		const folders = this.workspaceContextService.getWorkspace().folders;
		if (folders.length === 0) {
			return;
		}

		const eventsUri = URI.joinPath(
			folders[0].uri,
			...UI_EVENTS_FILE.split('/'),
		);

		// Ensure the file exists so the watcher has something to watch
		if (!await this.fileService.exists(eventsUri)) {
			await this.fileService.createFile(eventsUri, VSBuffer.fromString(''));
		}

		this._uiEventsOffset = (await this.fileService.stat(eventsUri)).size;

		const watcher = this._register(
			this.fileService.createWatcher(eventsUri, { recursive: false, excludes: [] })
		);

		this._register(watcher.onDidChange(async () => {
			try {
				const stat = await this.fileService.stat(eventsUri);
				if (stat.size <= this._uiEventsOffset) {
					return;
				}

				const file = await this.fileService.readFile(eventsUri);
				const text = file.value.toString();
				const lines = text.split('\n');

				// Work out which lines are new based on byte offset approximation
				const processed = text.slice(0, this._uiEventsOffset).split('\n').length - 1;
				this._uiEventsOffset = stat.size;

				for (const line of lines.slice(processed)) {
					if (!line.trim()) {
						continue;
					}
					try {
						const event = JSON.parse(line);
						await this.applyUiMutation(event);
					} catch {
						// Ignore malformed lines
					}
				}
			} catch (err) {
				this.logService.warn(`[SypherBus] UI events read error: ${err}`);
			}
		}));

		this.logService.info(`[SypherBus] Watching UI events at ${eventsUri.fsPath}`);
	}

	private async applyUiMutation(event: {
		action: string;
		// color_shift
		color?: string;
		// chassis_notification
		preset?: string;
		mode?: string;
		forced?: boolean;
		reason?: string;
		message?: string;
		// task_manifest_update
		manifest?: SypherTask[];
		// task_update
		step?: number;
		status?: SypherTaskStatus;
		output?: string;
		// forge_preview
		path?: string;
		type?: 'stl' | 'svg';
		name?: string;
		// planner_active
		active?: boolean;
	}): Promise<void> {
		this.logService.info(`[SypherBus] UI mutation: ${JSON.stringify(event)}`);

		switch (event.action) {
			case 'color_shift': {
				const color = event.color ?? '#ff2244';
				document.documentElement.style.setProperty('--sypher-neon', color);
				document.documentElement.style.setProperty('--sypher-neon-dim', color + '2e');
				document.documentElement.style.setProperty(
					'--sypher-neon-glow', `0 0 4px ${color}, 0 0 12px ${color}80`
				);
				break;
			}
			case 'chassis_notification': {
				const preset = event.preset ?? 'UNKNOWN';
				const reason = event.reason ?? '';
				const isSelfHeal = reason === 'self_heal';
				const label = isSelfHeal
					? localize('sypher.selfHeal', "SELF-HEAL: {0} ENGAGED - analysing build error", preset)
					: localize('sypher.autoMutation', "AUTO-MUTATION: {0} ENGAGED", preset);
				this.notificationService.notify({ severity: Severity.Info, message: label });

				const workbench = document.querySelector('.monaco-workbench');
				if (!workbench) { break; }

				// Strip any existing mode classes first
				workbench.classList.remove('sypher-godmode', 'sypher-fast-pulse');

				const mode = event.mode ?? (event.forced ? 'god_mode' : 'fast_mode');
				if (mode === 'god_mode') {
					workbench.classList.add('sypher-godmode');
					setTimeout(() => workbench.classList.remove('sypher-godmode'), 8000);
				} else {
					// Fast pulse fires 3 times (0.8s × 3 = 2.4s) then removes itself
					workbench.classList.add('sypher-fast-pulse');
					setTimeout(() => workbench.classList.remove('sypher-fast-pulse'), 2500);
				}
				break;
			}
			case 'task_manifest_update':
				if (event.manifest) {
					this.hudService.updateTaskManifest(event.manifest);
				}
				break;
			case 'task_update':
				if (event.step !== undefined && event.status) {
					this.hudService.updateTaskStatus(event.step, event.status, event.output);
				}
				break;
			case 'forge_preview':
				if (event.path && event.type && event.name) {
					await this.hudService.showForgePreview(event.path, event.type, event.name);
				}
				break;
			case 'planner_active':
				this.hudService.setPlannerActive(event.active ?? false);
				break;
			case 'maximize_terminal':
				await this.commandService.executeCommand('workbench.action.maximizePanel');
				break;
			case 'restore_layout':
				await this.commandService.executeCommand('workbench.action.evenEditorWidths');
				await this.commandService.executeCommand('workbench.action.toggleMaximizedPanel');
				break;
			case 'hide_explorer':
				await this.commandService.executeCommand('workbench.action.closeSidebar');
				break;
			case 'show_explorer':
				await this.commandService.executeCommand('workbench.action.toggleSidebarVisibility');
				break;
			case 'focus_terminal':
				await this.commandService.executeCommand('workbench.action.terminal.focus');
				break;
			default:
				this.logService.warn(`[SypherBus] Unknown UI mutation action: ${event.action}`);
		}
	}
}

// -- Ctrl+Shift+M - cycle SYPHER model preset ---------------------------------

class CycleSypherPresetAction extends Action2 {

	static readonly ID = 'sypher.cyclePreset';

	constructor() {
		super({
			id: CycleSypherPresetAction.ID,
			title: { value: localize('sypher.cyclePreset', "SYPHER: Cycle Model Preset"), original: 'SYPHER: Cycle Model Preset' },
			f1: true,
			keybinding: {
				primary: KeyMod.CtrlCmd | KeyMod.Shift | KeyCode.KeyM,
				weight: KeybindingWeight.WorkbenchContrib,
			},
		});
	}

	async run(accessor: ServicesAccessor): Promise<void> {
		const workspaceService = accessor.get(IWorkspaceContextService);
		const fileService = accessor.get(IFileService);
		const notificationService = accessor.get(INotificationService);
		const logService = accessor.get(ILogService);

		const folders = workspaceService.getWorkspace().folders;
		if (folders.length === 0) {
			notificationService.notify({
				severity: Severity.Warning,
				message: localize('sypher.noWorkspace', "SYPHER: No workspace folder open - cannot read .sypher/config.json"),
			});
			return;
		}

		const configUri = URI.joinPath(folders[0].uri, '.sypher', 'config.json');
		try {
			if (!await fileService.exists(configUri)) {
				notificationService.notify({
					severity: Severity.Warning,
					message: localize('sypher.noConfig', "SYPHER: .sypher/config.json not found - initialize the agent stack first"),
				});
				return;
			}

			const file = await fileService.readFile(configUri);
			const config = JSON.parse(file.value.toString());

			const order: string[] = config.preset_order ?? Object.keys(config.presets ?? {});
			const current: string = config.active_preset ?? order[0];
			const idx = order.indexOf(current);
			const nextName = order[(idx + 1) % order.length];

			config.active_preset = nextName;
			await fileService.writeFile(
				configUri,
				VSBuffer.fromString(JSON.stringify(config, null, 2)),
			);

			const preset = (config.presets as Record<string, { description?: string }>)[nextName];
			const description = preset?.description ?? nextName;

			notificationService.notify({
				severity: Severity.Info,
				message: localize(
					'sypher.chassisMutated',
					"CHASSIS MUTATED: {0} ACTIVE - {1}",
					nextName,
					description,
				),
			});

			logService.info(`[SypherBus] Preset cycled: ${current} → ${nextName}`);
		} catch (err) {
			logService.error(`[SypherBus] Preset cycle failed: ${err}`);
			notificationService.notify({
				severity: Severity.Error,
				message: localize('sypher.cycleError', "SYPHER: Failed to cycle preset - {0}", String(err)),
			});
		}
	}
}

// -- Registrations ----------------------------------------------------------

registerWorkbenchContribution2(
	SypherBusContribution.ID,
	SypherBusContribution,
	WorkbenchPhase.AfterRestored,
);

registerSingleton(ISypherHudService, SypherHudService, InstantiationType.Delayed);
registerSingleton(ISypherAgentService, SypherAgentService, InstantiationType.Delayed);

registerWorkbenchContribution2(
	SypherAgentLoader.ID,
	SypherAgentLoader,
	WorkbenchPhase.AfterRestored,
);

registerAction2(LoadSypherAgentAction);
registerAction2(CycleSypherPresetAction);
