const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const {test} = require("node:test");
const ts = require("typescript");
const React = require("react");
const {renderToStaticMarkup} = require("react-dom/server");
const {staticFile} = require("remotion");

// Render the real caption component with deterministic frame hooks. Browser
// rendering is unnecessary to check whether spaces sit outside word spans.
function loadSource(relativePath, remotion) {
  const source = fs.readFileSync(path.join(__dirname, "..", relativePath), "utf8");
  const compiled = ts.transpileModule(source, {
    compilerOptions: {
      module: ts.ModuleKind.CommonJS,
      jsx: ts.JsxEmit.ReactJSX,
      esModuleInterop: true,
    },
  }).outputText;
  const exports = {};
  vm.runInNewContext(compiled, {
    exports,
    require: (name) => name === "remotion" ? remotion : require(name),
  });
  return exports;
}

const {CaptionOverlay} = loadSource("src/components/CaptionOverlay.tsx", {
  AbsoluteFill: ({children, style}) => React.createElement("div", {style}, children),
  Sequence: ({children}) => React.createElement("section", null, children),
  useCurrentFrame: () => 0,
  useVideoConfig: () => ({fps: 30}),
  spring: () => 1,
  interpolate: () => 1,
});

const words = [
  {word: "First", startMs: 0, endMs: 500},
  {word: "line", startMs: 500, endMs: 1000},
];

test("English spacing is a sibling of the inline-block word", () => {
  const html = renderToStaticMarkup(React.createElement(CaptionOverlay, {words}));
  assert.match(html, /First<\/span> <span[^>]*>line<\/span>/);
});

test("an empty CJK separator introduces no spaces", () => {
  const html = renderToStaticMarkup(React.createElement(CaptionOverlay, {words, wordSeparator: ""}));
  assert.match(html, /First<\/span><span[^>]*>line<\/span>/);
});

test("explicit word boundaries split caption pages", () => {
  const html = renderToStaticMarkup(React.createElement(CaptionOverlay, {
    words: [{...words[0], pageBreakAfter: true}, words[1]],
  }));
  assert.equal((html.match(/<section>/g) || []).length, 2);
});

test("public-prefixed caption paths resolve relative to the public directory", () => {
  const {resolveAsset} = loadSource("src/lib/resolveAsset.ts", {staticFile});
  for (const prefix of ["", "public/", "./public/"]) {
    assert.equal(resolveAsset(`${prefix}talking-head/clip.mp4`), staticFile("talking-head/clip.mp4"));
  }
  assert.equal(resolveAsset("https://example.com/clip.mp4"), "https://example.com/clip.mp4");
  assert.equal(resolveAsset("/tmp/clip.mp4"), "file:///tmp/clip.mp4");
});
