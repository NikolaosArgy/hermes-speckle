import { configureTextBuilder } from 'troika-three-text'
import openSans from 'three/examples/fonts/open-sans/open-sans-v15-cyrillic-ext_greek_greek-ext_cyrillic_latin_latin-ext_vietnamese-regular.woff'

// troika (the viewer's text renderer) has no default font and would fetch fonts from
// cdn.jsdelivr.net; the bundled one covers Latin, Greek and Cyrillic offline. build.mjs patches
// troika so characters outside it stay in this font instead of going to the CDN.
configureTextBuilder({ defaultFontURL: openSans })

export * as V from '@speckle/viewer'
export * as OL from '@speckle/objectloader2'
