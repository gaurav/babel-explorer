const SVG_NS = "http://www.w3.org/2000/svg";
const SOURCE_COLORS = [
  "#c25735",
  "#0c6b63",
  "#d89b2b",
  "#2d6f9f",
  "#7a5d38",
  "#3f8458",
  "#a34662",
  "#54727e",
  "#9a7335",
  "#397d78",
  "#b35d32",
  "#516a3b",
];
const CLIQUE_COLORS = [
  "#78b7ad",
  "#e5a65a",
  "#7198c4",
  "#d17b77",
  "#9a86bd",
  "#83a95c",
  "#d08dac",
  "#72a6a9",
];
const UNRESOLVED_COLOR = "#a4aaa5";

const elements = {
  form: document.querySelector("#search-form"),
  input: document.querySelector("#query"),
  searchButton: document.querySelector("#search-button"),
  suggestions: document.querySelector("#suggestions"),
  graph: document.querySelector("#graph"),
  graphStage: document.querySelector("#graph-stage"),
  viewport: document.querySelector("#viewport"),
  edgeLayer: document.querySelector("#edge-layer"),
  nodeLayer: document.querySelector("#node-layer"),
  empty: document.querySelector("#empty-state"),
  loading: document.querySelector("#loading"),
  graphTitle: document.querySelector("#graph-title"),
  stats: document.querySelector("#stats"),
  release: document.querySelector("#release"),
  cliqueLegend: document.querySelector("#clique-legend"),
  legend: document.querySelector("#source-legend"),
  showAllSources: document.querySelector("#show-all-sources"),
  detail: document.querySelector("#selection-detail"),
  fitButton: document.querySelector("#fit-graph"),
  labelButton: document.querySelector("#toggle-labels"),
  recursionButton: document.querySelector("#toggle-recursion"),
  exampleButton: document.querySelector("#example-button"),
  loadingTitle: document.querySelector("#loading-title"),
  loadingDetail: document.querySelector("#loading-detail"),
  status: document.querySelector("#status"),
};

const state = {
  graph: null,
  graphs: { clique: null, recursive: null },
  currentQuery: "",
  currentLabel: "",
  positions: new Map(),
  enabledSources: new Set(),
  selectedElement: null,
  transform: { x: 0, y: 0, scale: 1 },
  graphRequest: null,
  searchRequest: null,
  searchTimer: null,
  showLabels: false,
  labelLayoutFrame: null,
  pan: null,
};

function setStatus(message, isError = false) {
  elements.status.textContent = message;
  elements.status.classList.toggle("is-error", isError);
}

async function getJson(url, signal) {
  const response = await fetch(url, { signal });
  let body;
  try {
    body = await response.json();
  } catch {
    throw new Error(`Request failed with HTTP ${response.status}`);
  }
  if (!response.ok) {
    throw new Error(body.error || `Request failed with HTTP ${response.status}`);
  }
  return body;
}

function looksLikeIdentifier(value) {
  return /^https?:\/\/\S+$/i.test(value) || /^[A-Za-z][A-Za-z0-9._-]*:\S+$/.test(value);
}

function compactType(types) {
  if (!types || types.length === 0) {
    return "untyped";
  }
  return types[0].replace(/^biolink:/, "");
}

function closeSuggestions() {
  elements.suggestions.hidden = true;
  elements.suggestions.replaceChildren();
  elements.input.setAttribute("aria-expanded", "false");
}

function renderSuggestionMessage(message) {
  const row = document.createElement("p");
  row.className = "suggestion-message";
  row.textContent = message;
  elements.suggestions.replaceChildren(row);
  elements.suggestions.hidden = false;
  elements.input.setAttribute("aria-expanded", "true");
}

function renderSuggestions(results) {
  elements.suggestions.replaceChildren();
  if (results.length === 0) {
    renderSuggestionMessage("No Name Resolver matches.");
    return;
  }

  for (const result of results) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "suggestion";
    button.setAttribute("role", "option");

    const label = document.createElement("span");
    label.className = "suggestion-label";
    label.textContent = result.label || result.curie;

    const curie = document.createElement("span");
    curie.className = "suggestion-curie";
    curie.textContent = result.curie;

    const meta = document.createElement("span");
    meta.className = "suggestion-meta";
    const identifierWord =
      result.clique_identifier_count === 1 ? "identifier" : "identifiers";
    meta.textContent =
      `${compactType(result.types)} · ${result.clique_identifier_count} ${identifierWord}`;

    button.append(label, curie, meta);
    button.addEventListener("click", () => {
      elements.input.value = result.curie;
      closeSuggestions();
      loadGraph(result.curie, result.label);
    });
    elements.suggestions.append(button);
  }
  elements.suggestions.hidden = false;
  elements.input.setAttribute("aria-expanded", "true");
}

async function resolveName(query) {
  if (state.searchRequest) {
    state.searchRequest.abort();
  }
  state.searchRequest = new AbortController();
  renderSuggestionMessage("Searching Name Resolver...");
  try {
    const payload = await getJson(
      `/api/resolve?query=${encodeURIComponent(query)}`,
      state.searchRequest.signal,
    );
    renderSuggestions(payload.results);
    setStatus(`${payload.results.length} Name Resolver matches for "${query}"`);
  } catch (error) {
    if (error.name === "AbortError") {
      return;
    }
    renderSuggestionMessage(error.message);
    setStatus(error.message, true);
  }
}

function paletteColor(value, palette) {
  let hash = 0;
  for (const character of value) {
    hash = (hash * 31 + character.charCodeAt(0)) >>> 0;
  }
  return palette[hash % palette.length];
}

function sourceColor(source) {
  return paletteColor(source, SOURCE_COLORS);
}

function cliqueColor(cliqueId) {
  return cliqueId
    ? paletteColor(cliqueId, CLIQUE_COLORS)
    : UNRESOLVED_COLOR;
}

function nodeGroupKey(node) {
  return node.clique_id || "__unresolved__";
}

function initialClusterLayout(graph) {
  const grouped = new Map();
  for (const node of graph.nodes) {
    const key = nodeGroupKey(node);
    if (!grouped.has(key)) {
      grouped.set(key, []);
    }
    grouped.get(key).push(node);
  }

  const queryNode = graph.nodes.find((node) => node.query);
  const queryGroup = queryNode ? nodeGroupKey(queryNode) : null;
  const groups = [...grouped.entries()].sort((a, b) => {
    if (a[0] === queryGroup) {
      return -1;
    }
    if (b[0] === queryGroup) {
      return 1;
    }
    return b[1].length - a[1].length || a[0].localeCompare(b[0]);
  });
  const outerGroups = Math.max(0, groups.length - 1);
  const clusterRadius = Math.max(430, (outerGroups * 300) / (2 * Math.PI));
  const centers = new Map();
  groups.forEach(([key], index) => {
    if (key === queryGroup || groups.length === 1) {
      centers.set(key, { x: 0, y: 0 });
      return;
    }
    const outerIndex = index - (queryGroup === null ? 0 : 1);
    const angle = -Math.PI / 2 + (outerIndex / outerGroups) * 2 * Math.PI;
    centers.set(key, {
      x: Math.cos(angle) * clusterRadius,
      y: Math.sin(angle) * clusterRadius,
    });
  });

  const positions = new Map();
  for (const [key, groupNodes] of groups) {
    const center = centers.get(key);
    groupNodes.sort(
      (a, b) =>
        Number(b.query) - Number(a.query) ||
        b.degree - a.degree ||
        a.id.localeCompare(b.id),
    );
    const movableNodes = groupNodes.filter((node) => !node.query);
    const localRadius = Math.max(105, (movableNodes.length * 62) / (2 * Math.PI));
    if (groupNodes[0]?.query) {
      positions.set(groupNodes[0].id, { ...center });
    }
    movableNodes.forEach((node, index) => {
      const angle =
        -Math.PI / 2 +
        (index / Math.max(1, movableNodes.length)) * 2 * Math.PI;
      positions.set(node.id, {
        x: center.x + Math.cos(angle) * localRadius,
        y: center.y + Math.sin(angle) * localRadius,
      });
    });
  }
  return { positions, centers };
}

function forceLayout(graph) {
  const { positions, centers } = initialClusterLayout(graph);
  const nodes = graph.nodes;
  if (nodes.length <= 1 || nodes.length > 350) {
    return positions;
  }

  const idealDistance = graph.mode === "recursive" ? 118 : 132;
  const repulsion = idealDistance * idealDistance * 0.85;
  const iterations = nodes.length <= 100 ? 340 : 220;

  for (let iteration = 0; iteration < iterations; iteration += 1) {
    const cooling = 1 - iteration / iterations;
    const forces = new Map(nodes.map((node) => [node.id, { x: 0, y: 0 }]));

    for (let leftIndex = 0; leftIndex < nodes.length; leftIndex += 1) {
      const left = nodes[leftIndex];
      const leftPosition = positions.get(left.id);
      for (
        let rightIndex = leftIndex + 1;
        rightIndex < nodes.length;
        rightIndex += 1
      ) {
        const right = nodes[rightIndex];
        const rightPosition = positions.get(right.id);
        let dx = rightPosition.x - leftPosition.x;
        let dy = rightPosition.y - leftPosition.y;
        if (dx === 0 && dy === 0) {
          dx = ((leftIndex + 1) * 17) % 11 - 5;
          dy = ((rightIndex + 1) * 19) % 13 - 6;
        }
        const distance = Math.max(1, Math.hypot(dx, dy));
        const magnitude = repulsion / distance;
        const forceX = (dx / distance) * magnitude;
        const forceY = (dy / distance) * magnitude;
        forces.get(left.id).x -= forceX;
        forces.get(left.id).y -= forceY;
        forces.get(right.id).x += forceX;
        forces.get(right.id).y += forceY;
      }
    }

    for (const edge of graph.edges) {
      const start = positions.get(edge.subject);
      const end = positions.get(edge.object);
      if (!start || !end) {
        continue;
      }
      const dx = end.x - start.x;
      const dy = end.y - start.y;
      const distance = Math.max(1, Math.hypot(dx, dy));
      const targetDistance = edge.cross_clique
        ? idealDistance * 1.65
        : idealDistance;
      const strength = edge.cross_clique ? 0.018 : 0.045;
      const magnitude = (distance - targetDistance) * strength;
      const forceX = (dx / distance) * magnitude;
      const forceY = (dy / distance) * magnitude;
      forces.get(edge.subject).x += forceX;
      forces.get(edge.subject).y += forceY;
      forces.get(edge.object).x -= forceX;
      forces.get(edge.object).y -= forceY;
    }

    for (const node of nodes) {
      const position = positions.get(node.id);
      const force = forces.get(node.id);
      const center = centers.get(nodeGroupKey(node)) || { x: 0, y: 0 };
      const clusterStrength =
        graph.mode === "recursive" && !node.in_query_clique ? 0.028 : 0.012;
      force.x += (center.x - position.x) * clusterStrength;
      force.y += (center.y - position.y) * clusterStrength;
      force.x -= position.x * 0.0015;
      force.y -= position.y * 0.0015;

      if (node.query) {
        position.x = 0;
        position.y = 0;
        continue;
      }
      const forceLength = Math.max(1, Math.hypot(force.x, force.y));
      const maxStep = 1.5 + cooling * 18;
      const step = Math.min(forceLength, maxStep);
      position.x += (force.x / forceLength) * step;
      position.y += (force.y / forceLength) * step;
    }
  }

  const query = nodes.find((node) => node.query);
  if (query) {
    const queryPosition = positions.get(query.id);
    for (const position of positions.values()) {
      position.x -= queryPosition.x;
      position.y -= queryPosition.y;
    }
  }
  return positions;
}

function edgePath(edge, parallelIndex, parallelCount) {
  const start = state.positions.get(edge.subject);
  const end = state.positions.get(edge.object);
  if (!start || !end) {
    return "";
  }
  if (edge.subject === edge.object) {
    return `M ${start.x} ${start.y - 18} C ${start.x + 48} ${start.y - 62}, ` +
      `${start.x + 48} ${start.y + 62}, ${start.x} ${start.y + 18}`;
  }

  const dx = end.x - start.x;
  const dy = end.y - start.y;
  const canonicalForward = edge.subject.localeCompare(edge.object) <= 0;
  const canonicalDx = canonicalForward ? dx : -dx;
  const canonicalDy = canonicalForward ? dy : -dy;
  const length = Math.hypot(canonicalDx, canonicalDy) || 1;
  const offset = (parallelIndex - (parallelCount - 1) / 2) * 30;
  const midX =
    (start.x + end.x) / 2 - (canonicalDy / length) * offset;
  const midY =
    (start.y + end.y) / 2 + (canonicalDx / length) * offset;
  return `M ${start.x} ${start.y} Q ${midX} ${midY} ${end.x} ${end.y}`;
}

function appendText(parent, className, value) {
  const element = document.createElement("p");
  element.className = className;
  element.textContent = value;
  parent.append(element);
}

function showNodeDetail(node) {
  elements.detail.replaceChildren();
  appendText(elements.detail, "detail-title", node.label || "Unlabeled identifier");
  appendText(elements.detail, "detail-curie", node.id);
  appendText(elements.detail, "detail-key", "NodeNorm clique");
  appendText(
    elements.detail,
    "detail-value",
    node.clique_id
      ? `${node.clique_label || "Unnamed clique"} · ${node.clique_id}`
      : "Not returned by NodeNorm",
  );
  appendText(elements.detail, "detail-key", "Graph role");
  appendText(
    elements.detail,
    "detail-value",
    node.in_query_clique ? "Starting clique" : "Added by recursive expansion",
  );
  appendText(elements.detail, "detail-key", "Distance from query");
  appendText(
    elements.detail,
    "detail-value",
    node.depth === null ? "Not connected" : String(node.depth),
  );
  appendText(elements.detail, "detail-key", "Incident edges");
  appendText(elements.detail, "detail-value", String(node.degree));
  appendText(elements.detail, "detail-key", "Biolink types");
  const types = document.createElement("div");
  types.className = "type-list";
  if (node.types.length === 0) {
    const none = document.createElement("span");
    none.className = "muted";
    none.textContent = "No type returned by NodeNorm";
    types.append(none);
  } else {
    for (const type of node.types) {
      const pill = document.createElement("span");
      pill.className = "type-pill";
      pill.textContent = type.replace(/^biolink:/, "");
      types.append(pill);
    }
  }
  elements.detail.append(types);
}

function showEdgeDetail(edge) {
  elements.detail.replaceChildren();
  appendText(elements.detail, "detail-title", edge.source);
  appendText(elements.detail, "detail-key", "Subject");
  appendText(elements.detail, "detail-value", edge.subject);
  appendText(elements.detail, "detail-key", "Predicate");
  appendText(elements.detail, "detail-value", edge.predicate);
  appendText(elements.detail, "detail-key", "Object");
  appendText(elements.detail, "detail-value", edge.object);
  appendText(elements.detail, "detail-key", "Clique relationship");
  appendText(
    elements.detail,
    "detail-value",
    edge.cross_clique
      ? "Connects different NodeNorm cliques"
      : edge.within_query_clique
        ? "Within the starting clique"
        : "Within another clique or unresolved",
  );
  appendText(elements.detail, "detail-key", "Babel provenance");
  appendText(elements.detail, "detail-value", edge.provenance);
}

function selectElement(element, kind, value) {
  if (state.selectedElement) {
    state.selectedElement.classList.remove("is-selected");
  }
  state.selectedElement = element;
  element.classList.add("is-selected");
  if (kind === "node") {
    showNodeDetail(value);
  } else {
    showEdgeDetail(value);
  }
  scheduleLabelLayout();
}

function parallelEdgeMetadata(edges) {
  const groups = new Map();
  for (const edge of edges) {
    const key = [edge.subject, edge.object].sort().join("\u0000");
    if (!groups.has(key)) {
      groups.set(key, []);
    }
    groups.get(key).push(edge);
  }
  const metadata = new Map();
  for (const group of groups.values()) {
    group.sort(
      (a, b) =>
        a.source.localeCompare(b.source) ||
        a.predicate.localeCompare(b.predicate) ||
        a.subject.localeCompare(b.subject) ||
        a.object.localeCompare(b.object),
    );
    group.forEach((edge, index) => {
      metadata.set(edge.id, { index, count: group.length });
    });
  }
  return metadata;
}

function applyLabelPosition(label, candidate) {
  label.setAttribute("y", String(candidate.y));
  label.style.textAnchor = candidate.anchor;
  label.querySelectorAll("tspan").forEach((line) => {
    line.setAttribute("x", String(candidate.x));
  });
}

function labelPositionCandidates(node, radius, position) {
  const sideY = node.label ? -5 : 4;
  const positions = {
    above: {
      x: 0,
      y: -(radius + (node.label ? 25 : 11)),
      anchor: "middle",
    },
    below: { x: 0, y: radius + 18, anchor: "middle" },
    left: { x: -(radius + 11), y: sideY, anchor: "end" },
    right: { x: radius + 11, y: sideY, anchor: "start" },
    upperLeft: {
      x: -(radius + 9),
      y: -(radius + (node.label ? 20 : 8)),
      anchor: "end",
    },
    upperRight: {
      x: radius + 9,
      y: -(radius + (node.label ? 20 : 8)),
      anchor: "start",
    },
    lowerLeft: {
      x: -(radius + 9),
      y: radius + 13,
      anchor: "end",
    },
    lowerRight: {
      x: radius + 9,
      y: radius + 13,
      anchor: "start",
    },
  };
  if (node.query) {
    return [
      positions.below,
      positions.right,
      positions.left,
      positions.above,
      positions.lowerRight,
      positions.lowerLeft,
      positions.upperRight,
      positions.upperLeft,
    ];
  }

  const angle = Math.atan2(position.y, position.x);
  const horizontal = Math.cos(angle);
  const vertical = Math.sin(angle);
  let preferred;
  if (Math.abs(horizontal) > 0.62) {
    preferred = horizontal > 0 ? "right" : "left";
  } else {
    preferred = vertical < 0 ? "above" : "below";
  }
  return [
    positions[preferred],
    positions.above,
    positions.below,
    positions.right,
    positions.left,
    positions.upperRight,
    positions.upperLeft,
    positions.lowerRight,
    positions.lowerLeft,
  ];
}

function positionNodeLabel(label, node, radius, position) {
  applyLabelPosition(
    label,
    labelPositionCandidates(node, radius, position)[0],
  );
}

function rectangleOverlap(left, right, padding = 4) {
  const width =
    Math.min(left.right, right.right) -
    Math.max(left.left, right.left) +
    padding * 2;
  const height =
    Math.min(left.bottom, right.bottom) -
    Math.max(left.top, right.top) +
    padding * 2;
  return Math.max(0, width) * Math.max(0, height);
}

function layoutNodeLabels() {
  if (!state.graph) {
    return;
  }
  const graphBounds = elements.graph.getBoundingClientRect();
  const nodeById = new Map(state.graph.nodes.map((node) => [node.id, node]));
  const labels = [...elements.nodeLayer.querySelectorAll(".graph-node")]
    .map((group) => ({
      group,
      label: group.querySelector(".node-label"),
      node: nodeById.get(group.dataset.nodeId),
    }))
    .filter(
      ({ label, node }) =>
        label &&
        node &&
        window.getComputedStyle(label).display !== "none",
    )
    .sort(
      (a, b) =>
        Number(b.node.query) - Number(a.node.query) ||
        b.node.degree - a.node.degree ||
        a.node.id.localeCompare(b.node.id),
    );

  const placed = [];
  for (const { group, label, node } of labels) {
    const radius = Number(group.dataset.radius);
    const position = state.positions.get(node.id);
    let bestCandidate = null;
    let bestScore = Number.POSITIVE_INFINITY;

    for (const candidate of labelPositionCandidates(
      node,
      radius,
      position,
    )) {
      applyLabelPosition(label, candidate);
      const bounds = label.getBoundingClientRect();
      const overlap = placed.reduce(
        (total, placedBounds) =>
          total + rectangleOverlap(bounds, placedBounds),
        0,
      );
      const overflow =
        Math.max(0, graphBounds.left + 6 - bounds.left) * bounds.height +
        Math.max(0, bounds.right - graphBounds.right + 6) * bounds.height +
        Math.max(0, graphBounds.top + 6 - bounds.top) * bounds.width +
        Math.max(0, bounds.bottom - graphBounds.bottom + 6) * bounds.width;
      const score = overlap * 4 + overflow;
      if (score < bestScore) {
        bestScore = score;
        bestCandidate = candidate;
      }
      if (score === 0) {
        break;
      }
    }
    applyLabelPosition(label, bestCandidate);
    placed.push(label.getBoundingClientRect());
  }
}

function scheduleLabelLayout() {
  if (state.labelLayoutFrame !== null) {
    cancelAnimationFrame(state.labelLayoutFrame);
  }
  state.labelLayoutFrame = requestAnimationFrame(() => {
    state.labelLayoutFrame = null;
    layoutNodeLabels();
  });
}

function drawGraph() {
  elements.edgeLayer.replaceChildren();
  elements.nodeLayer.replaceChildren();
  state.selectedElement = null;
  const graph = state.graph;
  const parallel = parallelEdgeMetadata(graph.edges);

  for (const edge of graph.edges) {
    const metadata = parallel.get(edge.id);
    const pathData = edgePath(edge, metadata.index, metadata.count);

    const path = document.createElementNS(SVG_NS, "path");
    path.classList.add("graph-edge");
    path.dataset.source = edge.source;
    path.dataset.subject = edge.subject;
    path.dataset.object = edge.object;
    path.setAttribute("d", pathData);
    path.setAttribute("stroke", sourceColor(edge.source));
    if (edge.cross_clique) {
      path.classList.add("cross-clique-edge");
    }

    const hit = document.createElementNS(SVG_NS, "path");
    hit.classList.add("graph-edge-hit");
    hit.dataset.source = edge.source;
    hit.setAttribute("d", pathData);
    hit.addEventListener("pointerdown", (event) => event.stopPropagation());
    hit.addEventListener("click", (event) => {
      event.stopPropagation();
      selectElement(path, "edge", edge);
    });

    const title = document.createElementNS(SVG_NS, "title");
    title.textContent =
      `${edge.subject} ${edge.predicate} ${edge.object}\nSource: ${edge.source}`;
    hit.append(title);
    elements.edgeLayer.append(path, hit);
  }

  for (const node of graph.nodes) {
    const position = state.positions.get(node.id);
    const group = document.createElementNS(SVG_NS, "g");
    group.classList.add("graph-node");
    if (node.in_query_clique) {
      group.classList.add("query-clique-node");
    }
    if (node.clique_leader) {
      group.classList.add("clique-leader");
    }
    if (node.query) {
      group.classList.add("query-node");
    } else if (!node.in_query_clique) {
      group.classList.add("expanded-node");
      group.style.setProperty("--clique-color", cliqueColor(node.clique_id));
    }
    group.dataset.nodeId = node.id;
    group.setAttribute("transform", `translate(${position.x} ${position.y})`);

    const circle = document.createElementNS(SVG_NS, "circle");
    const radius = node.query ? 18 : Math.min(15, 7 + Math.sqrt(node.degree) * 1.5);
    circle.setAttribute("r", String(radius));
    group.dataset.radius = String(radius);

    const label = document.createElementNS(SVG_NS, "text");
    label.classList.add("node-label");

    if (node.label) {
      const nameLine = document.createElementNS(SVG_NS, "tspan");
      nameLine.classList.add("node-label-name");
      nameLine.textContent =
        node.label.length > 34 ? `${node.label.slice(0, 32)}...` : node.label;
      label.append(nameLine);
    }

    const idLine = document.createElementNS(SVG_NS, "tspan");
    idLine.classList.add("node-label-id");
    if (node.label) {
      idLine.setAttribute("dy", "16");
    }
    idLine.textContent = node.id;
    label.append(idLine);
    positionNodeLabel(label, node, radius, position);

    const title = document.createElementNS(SVG_NS, "title");
    title.textContent = node.label ? `${node.label}\n${node.id}` : node.id;

    group.append(circle, label, title);
    group.addEventListener("pointerdown", (event) => event.stopPropagation());
    group.addEventListener("click", (event) => {
      event.stopPropagation();
      selectElement(group, "node", node);
    });
    elements.nodeLayer.append(group);
  }
  applySourceFilters();
}

function renderLegend(sources) {
  elements.legend.replaceChildren();
  state.enabledSources = new Set(sources.map((source) => source.name));

  for (const source of sources) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "source-toggle";
    button.dataset.source = source.name;
    button.setAttribute("aria-pressed", "true");

    const swatch = document.createElement("span");
    swatch.className = "source-swatch";
    swatch.style.backgroundColor = sourceColor(source.name);

    const name = document.createElement("span");
    name.className = "source-name";
    name.textContent = source.name;

    const count = document.createElement("span");
    count.className = "source-count";
    const edgeWord = source.edge_count === 1 ? "edge" : "edges";
    const nodeWord = source.node_count === 1 ? "identifier" : "identifiers";
    count.textContent = `${source.edge_count} ${edgeWord}`;
    button.title =
      `${source.edge_count} ${edgeWord} from ${source.name} provenance, ` +
      `touching ${source.node_count} ${nodeWord}. Click to toggle visibility.`;
    button.setAttribute(
      "aria-label",
      `${source.name}: ${source.edge_count} ${edgeWord}, ` +
        `${source.node_count} incident ${nodeWord}`,
    );

    button.append(swatch, name, count);
    button.addEventListener("click", () => {
      if (state.enabledSources.has(source.name)) {
        state.enabledSources.delete(source.name);
      } else {
        state.enabledSources.add(source.name);
      }
      button.classList.toggle("is-off", !state.enabledSources.has(source.name));
      button.setAttribute(
        "aria-pressed",
        state.enabledSources.has(source.name) ? "true" : "false",
      );
      applySourceFilters();
    });
    elements.legend.append(button);
  }
  elements.showAllSources.hidden = sources.length === 0;
}

function renderCliqueLegend(cliques) {
  elements.cliqueLegend.replaceChildren();
  for (const clique of cliques) {
    const row = document.createElement("div");
    row.className = "clique-key";

    const swatch = document.createElement("span");
    swatch.className = "clique-swatch";
    swatch.style.backgroundColor = clique.query
      ? "#f4c86f"
      : cliqueColor(clique.id);

    const text = document.createElement("span");
    text.className = "clique-name";
    const label = clique.id
      ? clique.label || clique.id
      : "Not returned by NodeNorm";
    text.textContent = clique.query ? `${label} · starting clique` : label;
    text.title = clique.id || "Unresolved";

    const count = document.createElement("span");
    count.className = "clique-count";
    count.textContent = String(clique.node_count);

    row.append(swatch, text, count);
    elements.cliqueLegend.append(row);
  }
}

function applySourceFilters() {
  if (!state.graph) {
    return;
  }
  const visibleDegree = new Map();
  document.querySelectorAll(".graph-edge, .graph-edge-hit").forEach((element) => {
    const visible = state.enabledSources.has(element.dataset.source);
    element.classList.toggle("is-hidden", !visible);
    if (
      visible &&
      element.classList.contains("graph-edge") &&
      element.dataset.subject
    ) {
      visibleDegree.set(
        element.dataset.subject,
        (visibleDegree.get(element.dataset.subject) || 0) + 1,
      );
      visibleDegree.set(
        element.dataset.object,
        (visibleDegree.get(element.dataset.object) || 0) + 1,
      );
    }
  });
  document.querySelectorAll(".graph-node").forEach((element) => {
    const node = state.graph.nodes.find((item) => item.id === element.dataset.nodeId);
    element.classList.toggle(
      "is-muted",
      !node.query && !visibleDegree.get(element.dataset.nodeId),
    );
  });
}

function updateTransform() {
  const { x, y, scale } = state.transform;
  elements.viewport.setAttribute("transform", `translate(${x} ${y}) scale(${scale})`);
  const inverseScale = 1 / scale;
  elements.nodeLayer.querySelectorAll(".graph-node circle").forEach((circle) => {
    circle.setAttribute("transform", `scale(${inverseScale})`);
  });
  elements.nodeLayer.querySelectorAll(".node-label").forEach((label) => {
    label.setAttribute("transform", `scale(${inverseScale})`);
  });
}

function fitGraph() {
  if (!state.graph || state.positions.size === 0) {
    return;
  }
  const rect = elements.graph.getBoundingClientRect();
  const xs = [...state.positions.values()].map((position) => position.x);
  const ys = [...state.positions.values()].map((position) => position.y);
  const minX = Math.min(...xs) - 80;
  const maxX = Math.max(...xs) + 80;
  const minY = Math.min(...ys) - 80;
  const maxY = Math.max(...ys) + 80;
  const width = Math.max(1, maxX - minX);
  const height = Math.max(1, maxY - minY);
  const scale = Math.min(rect.width / width, rect.height / height, 1.5) * 0.92;
  state.transform = {
    scale,
    x: rect.width / 2 - ((minX + maxX) / 2) * scale,
    y: rect.height / 2 - ((minY + maxY) / 2) * scale,
  };
  updateTransform();
  scheduleLabelLayout();
}

function renderStats(graph) {
  const values = [
    graph.nodes.length.toLocaleString(),
    graph.edges.length.toLocaleString(),
    graph.sources.length.toLocaleString(),
  ];
  elements.stats.querySelectorAll("strong").forEach((element, index) => {
    element.textContent = values[index];
  });
  const release = graph.babel_version
    ? `Babel ${graph.babel_version}`
    : "Babel release unavailable";
  const mode = graph.mode === "recursive" ? "full concordance" : "NodeNorm clique";
  elements.release.textContent = `${release} · ${mode}`;
}

function renderGraph(graph) {
  state.graph = graph;
  state.positions = forceLayout(graph);
  state.showLabels = graph.nodes.length <= 30;
  elements.graph.classList.toggle("show-labels", state.showLabels);
  elements.labelButton.setAttribute("aria-pressed", String(state.showLabels));
  elements.labelButton.disabled = false;
  elements.fitButton.disabled = false;
  elements.recursionButton.disabled = false;
  const expanded = graph.mode === "recursive";
  elements.recursionButton.textContent = expanded
    ? "Clique only"
    : "Show full concordance";
  elements.recursionButton.setAttribute("aria-pressed", String(expanded));
  elements.graphTitle.textContent = state.currentLabel
    ? `${state.currentLabel} · ${graph.query}`
    : graph.query;
  elements.empty.hidden = true;
  renderStats(graph);
  renderCliqueLegend(graph.cliques);
  renderLegend(graph.sources);
  drawGraph();
  fitGraph();

  const queryNode = graph.nodes.find((node) => node.query);
  if (queryNode) {
    const element = elements.nodeLayer.querySelector(
      `[data-node-id="${CSS.escape(queryNode.id)}"]`,
    );
    if (element) {
      selectElement(element, "node", queryNode);
    }
  }
}

async function loadGraphMode(curie, recurse) {
  if (state.graphRequest) {
    state.graphRequest.abort();
  }
  const controller = new AbortController();
  state.graphRequest = controller;
  elements.loading.hidden = false;
  elements.empty.hidden = true;
  elements.searchButton.disabled = true;
  elements.recursionButton.disabled = true;
  elements.graphTitle.textContent = state.currentLabel
    ? `${state.currentLabel} · ${curie}`
    : curie;
  elements.loadingTitle.textContent = recurse
    ? "Building the full concordance"
    : "Loading the NodeNorm clique";
  elements.loadingDetail.textContent = recurse
    ? "Following every reachable Babel mapping and grouping nodes by clique."
    : "Finding clique members and their internal Babel mappings.";
  setStatus(
    recurse
      ? `Building the full recursive concordance for ${curie}`
      : `Loading the NodeNorm clique for ${curie}`,
  );

  try {
    const started = await getJson(
      `/api/graph?curie=${encodeURIComponent(curie)}&recurse=${recurse}`,
      controller.signal,
    );
    let job = started;
    while (job.status === "queued" || job.status === "running") {
      setStatus(
        job.status === "queued"
          ? `Queued ${recurse ? "recursive concordance" : "clique"} query for ${curie}`
          : `Building ${recurse ? "recursive concordance" : "clique"} graph for ${curie}`,
      );
      await new Promise((resolve) => setTimeout(resolve, 750));
      job = await getJson(
        `/api/graph-status?job_id=${encodeURIComponent(job.job_id)}`,
        controller.signal,
      );
    }
    if (job.status === "failed") {
      throw new Error(job.error || "The graph query failed.");
    }
    const graph = job.graph;
    state.graphs[graph.mode] = graph;
    renderGraph(graph);
    setStatus(
      `Loaded ${graph.nodes.length} nodes and ${graph.edges.length} edges ` +
        `for ${graph.mode === "recursive" ? "the full concordance" : "the starting clique"}`,
    );
  } catch (error) {
    if (error.name === "AbortError") {
      return;
    }
    elements.empty.hidden = false;
    elements.empty.querySelector("h2").textContent = "The graph query failed.";
    elements.empty.querySelector("p:not(.empty-kicker)").textContent = error.message;
    setStatus(error.message, true);
  } finally {
    if (state.graphRequest === controller) {
      elements.loading.hidden = true;
      elements.searchButton.disabled = false;
      elements.recursionButton.disabled = !state.graph;
    }
  }
}

function loadGraph(curie, label = "") {
  closeSuggestions();
  state.currentQuery = curie;
  state.currentLabel = label;
  state.graphs = { clique: null, recursive: null };
  state.graph = null;
  elements.recursionButton.disabled = true;
  return loadGraphMode(curie, false);
}

elements.form.addEventListener("submit", (event) => {
  event.preventDefault();
  const value = elements.input.value.trim();
  if (!value) {
    setStatus("Enter an identifier or concept name.", true);
    elements.input.focus();
    return;
  }
  if (looksLikeIdentifier(value)) {
    loadGraph(value);
  } else {
    resolveName(value);
  }
});

elements.input.addEventListener("input", () => {
  clearTimeout(state.searchTimer);
  const value = elements.input.value.trim();
  if (looksLikeIdentifier(value) || value.length < 2) {
    closeSuggestions();
    return;
  }
  state.searchTimer = setTimeout(() => resolveName(value), 280);
});

elements.input.addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    closeSuggestions();
  }
});

document.addEventListener("click", (event) => {
  if (!elements.form.contains(event.target)) {
    closeSuggestions();
  }
});

elements.exampleButton.addEventListener("click", () => {
  elements.input.value = "MONDO:0004979";
  loadGraph("MONDO:0004979", "asthma");
});

elements.recursionButton.addEventListener("click", () => {
  if (!state.graph) {
    return;
  }
  if (state.graph.mode === "recursive") {
    renderGraph(state.graphs.clique);
    setStatus(`Showing only the starting NodeNorm clique for ${state.currentQuery}`);
    return;
  }
  if (state.graphs.recursive) {
    renderGraph(state.graphs.recursive);
    setStatus(`Showing the full recursive concordance for ${state.currentQuery}`);
    return;
  }
  loadGraphMode(state.currentQuery, true);
});

elements.fitButton.addEventListener("click", fitGraph);

elements.labelButton.addEventListener("click", () => {
  state.showLabels = !state.showLabels;
  elements.graph.classList.toggle("show-labels", state.showLabels);
  elements.labelButton.setAttribute("aria-pressed", String(state.showLabels));
  scheduleLabelLayout();
});

elements.showAllSources.addEventListener("click", () => {
  state.enabledSources = new Set(state.graph.sources.map((source) => source.name));
  document.querySelectorAll(".source-toggle").forEach((button) => {
    button.classList.remove("is-off");
    button.setAttribute("aria-pressed", "true");
  });
  applySourceFilters();
});

elements.graph.addEventListener(
  "wheel",
  (event) => {
    if (!state.graph) {
      return;
    }
    event.preventDefault();
    const rect = elements.graph.getBoundingClientRect();
    const pointerX = event.clientX - rect.left;
    const pointerY = event.clientY - rect.top;
    const worldX = (pointerX - state.transform.x) / state.transform.scale;
    const worldY = (pointerY - state.transform.y) / state.transform.scale;
    const nextScale = Math.min(
      6,
      Math.max(0.08, state.transform.scale * Math.exp(-event.deltaY * 0.0012)),
    );
    state.transform.x = pointerX - worldX * nextScale;
    state.transform.y = pointerY - worldY * nextScale;
    state.transform.scale = nextScale;
    updateTransform();
    scheduleLabelLayout();
  },
  { passive: false },
);

elements.graph.addEventListener("pointerdown", (event) => {
  if (!state.graph) {
    return;
  }
  state.pan = {
    pointerId: event.pointerId,
    startX: event.clientX,
    startY: event.clientY,
    originX: state.transform.x,
    originY: state.transform.y,
  };
  elements.graph.setPointerCapture(event.pointerId);
  elements.graph.classList.add("is-panning");
});

elements.graph.addEventListener("pointermove", (event) => {
  if (!state.pan || state.pan.pointerId !== event.pointerId) {
    return;
  }
  state.transform.x = state.pan.originX + event.clientX - state.pan.startX;
  state.transform.y = state.pan.originY + event.clientY - state.pan.startY;
  updateTransform();
});

elements.graph.addEventListener("pointerup", (event) => {
  if (!state.pan || state.pan.pointerId !== event.pointerId) {
    return;
  }
  state.pan = null;
  elements.graph.releasePointerCapture(event.pointerId);
  elements.graph.classList.remove("is-panning");
});

window.addEventListener("resize", () => {
  if (state.graph) {
    fitGraph();
  }
});
