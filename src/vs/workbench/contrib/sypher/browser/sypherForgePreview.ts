/*---------------------------------------------------------------------------------------------
 *  Copyright (c) Microsoft Corporation. All rights reserved.
 *  Licensed under the MIT License. See License.txt in the project root for license information.
 *--------------------------------------------------------------------------------------------*/

import { mainWindow } from '../../../../base/browser/window.js';
import { Disposable } from '../../../../base/common/lifecycle.js';
import { generateUuid } from '../../../../base/common/uuid.js';
import { URI } from '../../../../base/common/uri.js';
import { IFileService } from '../../../../platform/files/common/files.js';
import { createDecorator } from '../../../../platform/instantiation/common/instantiation.js';
import { ILogService } from '../../../../platform/log/common/log.js';
import { IWorkspaceContextService } from '../../../../platform/workspace/common/workspace.js';
import { IWebviewElement, IWebviewService, WebviewContentOptions, WebviewInitInfo, WebviewOptions } from '../../webview/browser/webview.js';

// -- Public types --------------------------------------------------------------

export interface SypherTask {
	readonly step: number;
	readonly task: string;
	status: 'pending' | 'running' | 'done' | 'failed';
	output?: string;
}

export type SypherTaskStatus = SypherTask['status'];

// -- Service interface ---------------------------------------------------------

export const ISypherHudService = createDecorator<ISypherHudService>('sypherHudService');

export interface ISypherHudService {
	readonly _serviceBrand: undefined;
	updateTaskManifest(tasks: SypherTask[]): void;
	updateTaskStatus(step: number, status: SypherTaskStatus, output?: string): void;
	showForgePreview(path: string, type: 'stl' | 'svg', name: string): Promise<void>;
	setPlannerActive(active: boolean): void;
}

// -- DOM helpers ---------------------------------------------------------------

function el<K extends keyof HTMLElementTagNameMap>(
	tag: K,
	classes: string,
	text?: string,
): HTMLElementTagNameMap[K] {
	const e = document.createElement(tag);
	e.className = classes;
	if (text !== undefined) {
		e.textContent = text;
	}
	return e;
}

const STEP_DOTS: Record<SypherTaskStatus, string> = {
	pending: '○',
	running: '◉',
	done:    '●',
	failed:  '✗',
};

// -- Service implementation ----------------------------------------------------

export class SypherHudService extends Disposable implements ISypherHudService {

	declare readonly _serviceBrand: undefined;

	private _taskSection: HTMLElement | undefined;
	private _taskItemsEl: HTMLElement | undefined;
	private _forgeSection: HTMLElement | undefined;
	private _forgeHost: HTMLElement | undefined;

	private readonly _taskEls = new Map<number, HTMLElement>();
	private _webview: IWebviewElement | undefined;

	constructor(
		@IWebviewService private readonly _webviewService: IWebviewService,
		@IFileService private readonly _fileService: IFileService,
		@IWorkspaceContextService private readonly _workspaceService: IWorkspaceContextService,
		@ILogService private readonly _log: ILogService,
	) {
		super();
		// Defer one frame - ensures the workbench DOM is painted before injection.
		requestAnimationFrame(() => this._mountHud());
	}

	// -- HUD DOM scaffolding ---------------------------------------------------

	private _mountHud(): void {
		const workbench = document.querySelector<HTMLElement>('.monaco-workbench');
		if (!workbench) {
			return;
		}

		const root = el('div', 'sypher-hud-root');

		// -- Task manifest section -------------------------------------------
		const taskSection = el('div', 'sypher-hud-section sypher-task-list');
		taskSection.style.display = 'none';
		taskSection.appendChild(el('div', 'sypher-hud-header', '◈ TASK MANIFEST'));
		const taskItemsEl = el('div', 'sypher-task-items');
		taskSection.appendChild(taskItemsEl);

		// -- Forge preview section -------------------------------------------
		const forgeSection = el('div', 'sypher-hud-section sypher-forge-panel');
		forgeSection.style.display = 'none';
		forgeSection.appendChild(el('div', 'sypher-hud-header', '⬡ FORGE PREVIEW'));
		const forgeHost = el('div', 'sypher-forge-host');
		forgeSection.appendChild(forgeHost);

		root.append(taskSection, forgeSection);
		workbench.appendChild(root);

		this._taskSection = taskSection;
		this._taskItemsEl = taskItemsEl;
		this._forgeSection = forgeSection;
		this._forgeHost = forgeHost;
	}

	// -- Task list -------------------------------------------------------------

	updateTaskManifest(tasks: SypherTask[]): void {
		const container = this._taskItemsEl;
		if (!container) {
			return;
		}
		if (this._taskSection) {
			this._taskSection.style.display = 'flex';
		}

		container.innerHTML = '';
		this._taskEls.clear();

		for (const t of tasks) {
			const row = this._buildTaskRow(t);
			this._taskEls.set(t.step, row);
			container.appendChild(row);
		}
	}

	updateTaskStatus(step: number, status: SypherTaskStatus, output?: string): void {
		const row = this._taskEls.get(step);
		if (!row) {
			return;
		}
		row.className = `sypher-task-item ${status}`;

		const dot = row.querySelector<HTMLElement>('.sypher-task-dot');
		if (dot) {
			dot.textContent = STEP_DOTS[status];
		}

		if (output) {
			let outEl = row.querySelector<HTMLElement>('.sypher-task-output');
			if (!outEl) {
				outEl = el('div', 'sypher-task-output');
				row.appendChild(outEl);
			}
			outEl.textContent = output.length > 80 ? output.slice(0, 77) + '…' : output;
		}
	}

	private _buildTaskRow(task: SypherTask): HTMLElement {
		const row = el('div', `sypher-task-item ${task.status}`);

		const dot = el('span', 'sypher-task-dot', STEP_DOTS[task.status]);
		const num = el('span', 'sypher-task-num', String(task.step).padStart(2, '0'));
		const label = el('span', 'sypher-task-label', task.task);

		row.append(dot, num, label);
		return row;
	}

	// -- Planner active glow ---------------------------------------------------

	setPlannerActive(active: boolean): void {
		const wb = document.querySelector('.monaco-workbench');
		if (!wb) {
			return;
		}
		if (active) {
			wb.classList.add('sypher-godmode');
		} else {
			wb.classList.remove('sypher-godmode');
		}
	}

	// -- Forge preview webview -------------------------------------------------

	async showForgePreview(path: string, type: 'stl' | 'svg', name: string): Promise<void> {
		if (this._forgeSection) {
			this._forgeSection.style.display = 'flex';
		}

		if (!this._webview && this._forgeHost) {
			this._webview = this._register(this._createWebview());
			this._webview.mountTo(this._forgeHost, mainWindow);
		}

		if (!this._webview) {
			return;
		}

		try {
			const fileUri = this._resolveForgeUri(path);
			const file = await this._fileService.readFile(fileUri);

			if (type === 'stl') {
				// Convert Uint8Array → plain number array for structured-clone serialization
				const bytes = Array.from(file.value.buffer);
				await this._webview.postMessage({ type: 'stl', name, data: bytes });
			} else {
				await this._webview.postMessage({ type: 'svg', name, data: file.value.toString() });
			}

			this._log.info(`[SypherHud] Forge preview loaded: ${name} (${type})`);
		} catch (err) {
			this._log.warn(`[SypherHud] Forge preview failed for '${path}': ${err}`);
		}
	}

	private _resolveForgeUri(path: string): URI {
		if (path.startsWith('/')) {
			return URI.file(path);
		}
		const folders = this._workspaceService.getWorkspace().folders;
		if (folders.length === 0) {
			return URI.file(path);
		}
		return URI.joinPath(folders[0].uri, ...path.split('/'));
	}

	private _createWebview(): IWebviewElement {
		const nonce = generateUuid().replace(/-/g, '');

		const options: WebviewOptions = { retainContextWhenHidden: true };
		const contentOptions: WebviewContentOptions = { allowScripts: true };
		const initInfo: WebviewInitInfo = {
			providedViewType: 'sypher.forgePreview',
			title: 'Forge Preview',
			options,
			contentOptions,
			extension: undefined,
		};

		const webview = this._webviewService.createWebviewElement(initInfo);
		webview.setHtml(this._buildForgeHtml(nonce));
		return webview;
	}

	private _buildForgeHtml(nonce: string): string {
		return /* html */`<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'nonce-${nonce}' https://cdn.jsdelivr.net; style-src 'unsafe-inline'; img-src blob:; connect-src 'none';">
<style>
* { margin:0; padding:0; box-sizing:border-box; }
html,body { width:100%; height:100%; background:transparent; overflow:hidden; }
canvas { display:block; width:100%!important; height:100%!important; }
#status {
  position:absolute; bottom:6px; left:0; right:0; text-align:center;
  font:9px/1.4 monospace; letter-spacing:.12em; color:#ff2244;
  text-shadow:0 0 6px #ff2244; pointer-events:none; text-transform:uppercase;
}
#loading {
  position:absolute; top:50%; left:50%; transform:translate(-50%,-50%);
  font:10px/1.4 monospace; color:#ff2244; letter-spacing:.15em; text-transform:uppercase;
  animation:blink 1.2s step-start infinite;
}
@keyframes blink { 0%,100%{opacity:1;} 50%{opacity:0;} }
</style>
</head>
<body>
<canvas id="c"></canvas>
<div id="loading">⬡ AWAITING FORGE OUTPUT...</div>
<div id="status"></div>

<script type="importmap" nonce="${nonce}">
{"imports":{"three":"https://cdn.jsdelivr.net/npm/three@0.165.0/build/three.module.js","three/addons/":"https://cdn.jsdelivr.net/npm/three@0.165.0/examples/jsm/"}}
</script>
<script type="module" nonce="${nonce}">
import * as THREE from 'three';
import { STLLoader } from 'three/addons/loaders/STLLoader.js';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';

const canvas  = document.getElementById('c');
const statusEl = document.getElementById('status');
const loadingEl = document.getElementById('loading');

// -- Renderer ------------------------------------------------------------------
const renderer = new THREE.WebGLRenderer({ canvas, antialias:true, alpha:true });
renderer.setPixelRatio(window.devicePixelRatio);
renderer.shadowMap.enabled = true;
renderer.shadowMap.type = THREE.PCFSoftShadowMap;

// -- Scene ---------------------------------------------------------------------
const scene = new THREE.Scene();

// -- Camera --------------------------------------------------------------------
const camera = new THREE.PerspectiveCamera(55, 1, 0.001, 2000);
camera.position.set(5, 4, 6);

// -- Controls ------------------------------------------------------------------
const controls = new OrbitControls(camera, renderer.domElement);
controls.enableDamping = true;
controls.dampingFactor = 0.06;
controls.autoRotate = true;
controls.autoRotateSpeed = 0.6;

// -- Lighting ------------------------------------------------------------------
scene.add(new THREE.AmbientLight(0x160606, 4));

const neonLight = new THREE.DirectionalLight(0xff2244, 5);
neonLight.position.set(3, 5, 3);
neonLight.castShadow = true;
neonLight.shadow.mapSize.set(1024, 1024);
scene.add(neonLight);

const rimLight = new THREE.DirectionalLight(0x00eeff, 2.5);
rimLight.position.set(-4, 2, -3);
scene.add(rimLight);

const fillLight = new THREE.DirectionalLight(0xffcc00, 0.8);
fillLight.position.set(0, -3, 2);
scene.add(fillLight);

// -- Grid ----------------------------------------------------------------------
const grid = new THREE.GridHelper(16, 32, 0xff2244, 0x330011);
grid.position.y = -0.01;
scene.add(grid);

// -- Placeholder wireframe cube ------------------------------------------------
const placeholderEdges = new THREE.EdgesGeometry(new THREE.BoxGeometry(2,2,2));
const placeholder = new THREE.LineSegments(
  placeholderEdges,
  new THREE.LineBasicMaterial({ color:0xff2244, transparent:true, opacity:0.25 })
);
scene.add(placeholder);

// -- Model slot ----------------------------------------------------------------
let modelGroup = null;
const stlLoader = new STLLoader();

function clearModel() {
  if (modelGroup) {
    scene.remove(modelGroup);
    modelGroup.traverse(c => { if (c.geometry) c.geometry.dispose(); });
    modelGroup = null;
  }
}

function loadSTL(buffer) {
  clearModel();
  scene.remove(placeholder);
  loadingEl.style.display = 'none';

  const geometry = stlLoader.parse(buffer);
  geometry.computeVertexNormals();
  geometry.center();

  const body = new THREE.MeshPhongMaterial({
    color: 0x0d0305, specular: 0xff2244, emissive: 0x0a0000, shininess: 90
  });
  const mesh = new THREE.Mesh(geometry, body);
  mesh.castShadow = true;
  mesh.receiveShadow = true;

  // Neon wireframe overlay
  const wire = new THREE.LineSegments(
    new THREE.WireframeGeometry(geometry),
    new THREE.LineBasicMaterial({ color: 0xff2244, transparent: true, opacity: 0.12 })
  );
  mesh.add(wire);

  // Fit to view
  const box = new THREE.Box3().setFromObject(mesh);
  const size = box.getSize(new THREE.Vector3());
  const maxDim = Math.max(size.x, size.y, size.z);
  if (maxDim > 0) { mesh.scale.setScalar(4 / maxDim); }

  grid.position.y = box.min.y * (4 / maxDim) - 0.02;

  modelGroup = new THREE.Group();
  modelGroup.add(mesh);
  scene.add(modelGroup);
  controls.autoRotate = true;
}

function loadSVG(svgStr, name) {
  clearModel();
  scene.remove(placeholder);
  loadingEl.style.display = 'none';

  const blob = new Blob([svgStr], { type:'image/svg+xml' });
  const url  = URL.createObjectURL(blob);
  const img  = new Image();

  img.onload = () => {
    const cvs = document.createElement('canvas');
    cvs.width = img.width; cvs.height = img.height;
    cvs.getContext('2d').drawImage(img, 0, 0);

    const tex = new THREE.CanvasTexture(cvs);
    const aspect = img.height / img.width;
    const geo = new THREE.PlaneGeometry(4, 4 * aspect);
    const mat = new THREE.MeshBasicMaterial({ map:tex, side:THREE.DoubleSide, transparent:true });
    const plane = new THREE.Mesh(geo, mat);

    modelGroup = new THREE.Group();
    modelGroup.add(plane);
    scene.add(modelGroup);

    camera.position.set(0, 0, 5);
    controls.autoRotate = false;
    URL.revokeObjectURL(url);
    statusEl.textContent = '⬡ ' + name + ' - PCB ONLINE';
  };
  img.src = url;
}

// -- Resize --------------------------------------------------------------------
function resize() {
  const w = window.innerWidth, h = window.innerHeight;
  renderer.setSize(w, h, false);
  camera.aspect = w / h;
  camera.updateProjectionMatrix();
}
window.addEventListener('resize', resize);
resize();

// -- Message bus ---------------------------------------------------------------
window.addEventListener('message', e => {
  const msg = e.data;
  if (!msg?.type) { return; }

  if (msg.type === 'stl') {
    statusEl.textContent = '⬡ LOADING ' + (msg.name || 'model') + '...';
    const buf = new Uint8Array(msg.data).buffer;
    loadSTL(buf);
    statusEl.textContent = '⬡ ' + (msg.name || 'MODEL') + ' - ONLINE';
  } else if (msg.type === 'svg') {
    statusEl.textContent = '⬡ RENDERING PCB...';
    loadSVG(msg.data, msg.name || 'PCB');
  }
});

// -- Render loop ---------------------------------------------------------------
(function loop() {
  requestAnimationFrame(loop);
  controls.update();
  renderer.render(scene, camera);
})();
</script>
</body>
</html>`;
	}
}
