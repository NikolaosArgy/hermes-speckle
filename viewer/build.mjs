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
const readText = (path) => readFileSync(path, 'utf8').replace(/\r\n/g, '\n').trim()

function notices(inputs) {
  const dirs = [...new Set(Object.keys(inputs).map(packageDirOf).filter(Boolean))]
  const entries = dirs.map((dir) => {
    const abs = join(here, dir)
    const pkg = JSON.parse(readFileSync(join(abs, 'package.json'), 'utf8'))
    const files = readdirSync(abs).filter((f) => LICENCE_FILE.test(f)).sort()
    const texts = files.map((f) => readText(join(abs, f)))
    if (!texts.length && LICENCE_OVERRIDES[pkg.name]) texts.push(readText(join(here, LICENCE_OVERRIDES[pkg.name])))
    return { name: pkg.name, version: pkg.version, license: pkg.license || 'UNKNOWN', texts }
  })
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
    `The viewer bundle contains code from the ${entries.length} packages below, built from`,
    'viewer/package-lock.json. Each package keeps its own licence, reproduced here.',
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
// without one stops the build until it gets an entry in LICENCE_OVERRIDES.
if (missing.length) {
  console.error(`no licence text for: ${missing.map((m) => m.name).join(', ')}; add it to LICENCE_OVERRIDES`)
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
