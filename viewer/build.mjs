// Builds dist/speckle-viewer.js (one ESM file: @speckle/viewer, @speckle/objectloader2 and what
// they import) and dist/THIRD_PARTY_NOTICES.txt (the licence of every package in it).
//
//   npm ci && node build.mjs           write dist/
//   npm ci && node build.mjs --check   rebuild in memory, exit 1 if dist/ differs
//
// The plugin ships the bundle rather than loading the viewer from a CDN at runtime, so the
// viewer works offline and behind proxies, and reviewers can rebuild it from the lockfile.
import { createHash } from 'node:crypto'
import { existsSync, readdirSync, readFileSync, writeFileSync, mkdirSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import * as esbuild from 'esbuild'

const here = dirname(fileURLToPath(import.meta.url))
const dist = join(here, 'dist')
const BUNDLE = 'speckle-viewer.js'
const NOTICES = 'THIRD_PARTY_NOTICES.txt'

// troika (the viewer's text renderer) sends any character its loaded fonts don't cover to a font
// resolver on cdn.jsdelivr.net, falling back to that CDN even when unicodeFontsURL points elsewhere,
// and when the resolver can't be reached the text never finishes and the whole model load hangs.
// The patch keeps such characters in the bundled default font (they draw as blank glyphs), so the
// viewer never contacts a CDN and offline loads complete. Reviewed in hermes-agent PR #152842.
const TROIKA_FALLBACK = /function resolveFallbacks\(\) \{\n(\s*)if \(fallbackRanges\.length\) \{/
const TROIKA_OFFLINE = (_match, indent) => `function resolveFallbacks() {
${indent}if (fallbackRanges.length && userFonts.length) {
${indent}  const fontObj = parsedFonts[userFonts[0].src];
${indent}  let fontIndex = fontIndices.get(fontObj);
${indent}  if (typeof fontIndex !== 'number') {
${indent}    fontIndex = fontResolutions.length;
${indent}    fontResolutions.push(fontObj);
${indent}    fontIndices.set(fontObj, fontIndex);
${indent}  }
${indent}  fallbackRanges.forEach(([start, end]) => {
${indent}    for (let i = start; i <= end; i++) charResolutions[i] = fontIndex;
${indent}  });
${indent}  allDone();
${indent}} else if (fallbackRanges.length) {`

const troikaOffline = {
  name: 'troika-offline-fallback',
  setup(build) {
    build.onLoad({ filter: /troika-three-text[\\/]dist[\\/]troika-three-text\.esm\.js$/ }, (args) => {
      const source = readFileSync(args.path, 'utf8')
      const matches = source.match(new RegExp(TROIKA_FALLBACK.source, 'g')) || []
      if (matches.length !== 1) {
        throw new Error(`troika-offline-fallback: expected 1 resolveFallbacks() to patch, found ${matches.length}; review the troika upgrade`)
      }
      return { contents: source.replace(TROIKA_FALLBACK, TROIKA_OFFLINE), loader: 'js' }
    })
  },
}

const result = await esbuild.build({
  absWorkingDir: here,
  entryPoints: ['entry.js'],
  bundle: true,
  format: 'esm',
  platform: 'browser',
  target: 'chrome120',
  // Unminified so catalog reviewers can read it (rule 17), and because minifying joins template
  // strings onto one line, where the install scanner's inline_shell_exec pattern misfires.
  minify: false,
  // Licence texts go in THIRD_PARTY_NOTICES.txt in full instead of scattered banner comments.
  legalComments: 'none',
  charset: 'utf8',
  // The default text font is embedded so the viewer never downloads one (see entry.js).
  loader: { '.woff': 'dataurl' },
  plugins: [troikaOffline],
  metafile: true,
  write: false,
  outfile: join(dist, BUNDLE),
  logLevel: 'warning',
  banner: {
    js: `/*! hermes-speckle viewer bundle: @speckle/viewer and its dependencies, built by viewer/build.mjs from viewer/package-lock.json. Licences: ${NOTICES} */`,
  },
})

function packageDirOf(input) {
  const parts = input.split('/')
  const at = parts.lastIndexOf('node_modules')
  if (at < 0) return null
  const name = parts[at + 1].startsWith('@') ? parts.slice(at + 1, at + 3) : [parts[at + 1]]
  return parts.slice(0, at + 1 + name.length).join('/')
}

const LICENCE_FILE = /^(.*[-_.])?(licen[cs]e|copying|notice)(\.(md|txt|markdown))?$/i
// The @speckle packages publish without a licence file; they come from speckle-server, whose
// LICENSE (fetched at the matching release tag) puts them under Apache-2.0.
const LICENCE_OVERRIDES = {
  '@speckle/objectloader2': 'licenses/speckle-server.LICENSE.txt',
  '@speckle/shared': 'licenses/speckle-server.LICENSE.txt',
  '@speckle/viewer': 'licenses/speckle-server.LICENSE.txt',
}
// Embedded asset files carry their own licence, which can differ from their package's: three.js is
// MIT, the Open Sans font it ships is Apache-2.0 (Google), per the font's own name table.
const ASSET_NOTICES = {
  'node_modules/three/examples/fonts/open-sans/open-sans-v15-cyrillic-ext_greek_greek-ext_cyrillic_latin_latin-ext_vietnamese-regular.woff': {
    name: 'Open Sans (font, from three/examples/fonts)', version: '1.10', license: 'Apache-2.0',
    file: 'licenses/open-sans.LICENSE.txt',
  },
}
const ASSET = /\.(woff2?|ttf|otf|png|jpe?g|gif|svg|wasm)$/i
const readText = (path) => readFileSync(path, 'utf8').replace(/\r\n/g, '\n').trim()

function notices(inputs) {
  const dirs = [...new Set(Object.keys(inputs).map(packageDirOf).filter(Boolean))]
  const assets = Object.keys(inputs).filter((input) => ASSET.test(input)).map((input) => {
    const notice = ASSET_NOTICES[input]
    if (!notice) return { name: input, version: '-', license: 'UNKNOWN', texts: [] }
    return { name: notice.name, version: notice.version, license: notice.license, texts: [readText(join(here, notice.file))] }
  })
  const packages = dirs.map((dir) => {
    const abs = join(here, dir)
    const pkg = JSON.parse(readFileSync(join(abs, 'package.json'), 'utf8'))
    const files = readdirSync(abs).filter((f) => LICENCE_FILE.test(f)).sort()
    const texts = files.map((f) => readText(join(abs, f)))
    if (!texts.length && LICENCE_OVERRIDES[pkg.name]) texts.push(readText(join(here, LICENCE_OVERRIDES[pkg.name])))
    return { name: pkg.name, version: pkg.version, license: pkg.license || 'UNKNOWN', texts }
  })
  const entries = [...packages, ...assets]
  entries.sort((a, b) => (a.name < b.name ? -1 : a.name > b.name ? 1 : 0))
  const missing = entries.filter((e) => e.texts.length === 0)
  const rule = '='.repeat(78)
  const body = entries.map((e) => [
    rule,
    `${e.name}@${e.version}  (${e.license})`,
    rule,
    e.texts.join('\n\n'),
  ].join('\n'))
  const head = [
    'Third-party software in dist/speckle-viewer.js',
    '',
    `The viewer bundle contains code from the ${packages.length} packages below${assets.length ? ` and ${assets.length} embedded asset file(s)` : ''},`,
    'built from viewer/package-lock.json. Each keeps its own licence, reproduced here.',
    '',
    entries.map((e) => `  ${e.name}@${e.version}  ${e.license}`).join('\n'),
    '',
  ].join('\n')
  return { text: head + '\n' + body.join('\n\n') + '\n', missing }
}

const bundle = Buffer.from(result.outputFiles[0].contents)
const { text, missing } = notices(result.metafile.inputs)
const outputs = { [BUNDLE]: bundle, [NOTICES]: Buffer.from(text, 'utf8') }
const sha = (b) => createHash('sha256').update(b).digest('hex')

// Shipping code without its licence text breaks MIT/Apache-2.0 terms, so a new dependency
// without one stops the build until it gets an entry in LICENCE_OVERRIDES (or ASSET_NOTICES).
if (missing.length) {
  console.error(`no licence text for: ${missing.map((m) => m.name).join(', ')}; add it to LICENCE_OVERRIDES or ASSET_NOTICES`)
  process.exit(1)
}

if (process.argv.includes('--check')) {
  let ok = true
  for (const [name, bytes] of Object.entries(outputs)) {
    const path = join(dist, name)
    const onDisk = existsSync(path) ? readFileSync(path) : null
    if (!onDisk || !onDisk.equals(bytes)) {
      ok = false
      console.error(`dist/${name} does not match a fresh build (committed ${onDisk ? sha(onDisk) : 'missing'}, built ${sha(bytes)})`)
    }
  }
  if (!ok) {
    console.error('Run `npm ci && node build.mjs` in viewer/ and commit dist/.')
    process.exit(1)
  }
  console.log(`dist/ matches a fresh build: ${BUNDLE} sha256 ${sha(bundle)}`)
} else {
  mkdirSync(dist, { recursive: true })
  for (const [name, bytes] of Object.entries(outputs)) writeFileSync(join(dist, name), bytes)
  console.log(`wrote dist/${BUNDLE} (${(bundle.length / 1e6).toFixed(2)} MB, sha256 ${sha(bundle)}) and dist/${NOTICES}`)
}
