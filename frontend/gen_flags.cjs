const fs = require('fs')
const path = require('path')

const OUT = path.resolve(__dirname, 'public', 'flags')
fs.mkdirSync(OUT, { recursive: true })

// 5-point star polygon path
function star(cx, cy, R, r, rotDeg = 0) {
  const rot = (rotDeg - 90) * Math.PI / 180
  const pts = []
  for (let i = 0; i < 10; i++) {
    const ang = rot + i * Math.PI / 5
    const rad = (i % 2 === 0) ? R : r
    pts.push(`${(cx + rad * Math.cos(ang)).toFixed(2)},${(cy + rad * Math.sin(ang)).toFixed(2)}`)
  }
  return 'M' + pts.join(' L') + ' Z'
}

const flags = {}

// ---- China (3:2) ----
;(function () {
  const W = 30, H = 20
  let s = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${W} ${H}" width="${W}" height="${H}">`
  s += `<rect width="${W}" height="${H}" fill="#DE2910"/>`
  s += `<path d="${star(5, 5, 3, 1.2, 0)}" fill="#FFDE00"/>`
  const smalls = [
    { c: [10, 2], rot: 239 },
    { c: [12, 4], rot: 261.9 },
    { c: [12, 7], rot: 285.9 },
    { c: [10, 9], rot: 308.7 },
  ]
  for (const m of smalls) {
    s += `<path d="${star(m.c[0], m.c[1], 1, 0.4, m.rot)}" fill="#FFDE00"/>`
  }
  s += `</svg>`
  flags['cn'] = s
})()

// ---- United Kingdom (3:2) ----
;(function () {
  const W = 60, H = 30
  let s = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${W} ${H}" width="${W}" height="${H}">`
  s += `<rect width="${W}" height="${H}" fill="#012169"/>`
  // white diagonals
  s += `<path d="M0,0 L${W},${H}" stroke="#fff" stroke-width="12"/>`
  s += `<path d="M${W},0 L0,${H}" stroke="#fff" stroke-width="12"/>`
  // red diagonals
  s += `<path d="M0,0 L${W},${H}" stroke="#C8102E" stroke-width="4"/>`
  s += `<path d="M${W},0 L0,${H}" stroke="#C8102E" stroke-width="4"/>`
  // white cross
  s += `<rect x="24" y="0" width="12" height="${H}" fill="#fff"/>`
  s += `<rect x="0" y="9" width="${W}" height="12" fill="#fff"/>`
  // red cross
  s += `<rect x="27" y="0" width="6" height="${H}" fill="#C8102E"/>`
  s += `<rect x="0" y="12" width="${W}" height="6" fill="#C8102E"/>`
  s += `</svg>`
  flags['gb'] = s
})()

// ---- Germany (3:2, official 5:3 but fine) ----
;(function () {
  const W = 30, H = 20
  let s = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${W} ${H}" width="${W}" height="${H}">`
  s += `<rect width="${W}" height="${H / 3}" y="0" fill="#000000"/>`
  s += `<rect width="${W}" height="${H / 3}" y="${H / 3}" fill="#DD0000"/>`
  s += `<rect width="${W}" height="${H / 3}" y="${(2 * H) / 3}" fill="#FFCE00"/>`
  s += `</svg>`
  flags['de'] = s
})()

// ---- France (3:2) ----
;(function () {
  const W = 30, H = 20
  const bw = W / 3
  let s = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${W} ${H}" width="${W}" height="${H}">`
  s += `<rect x="0" width="${bw}" height="${H}" fill="#0055A4"/>`
  s += `<rect x="${bw}" width="${bw}" height="${H}" fill="#FFFFFF"/>`
  s += `<rect x="${2 * bw}" width="${bw}" height="${H}" fill="#EF4135"/>`
  s += `</svg>`
  flags['fr'] = s
})()

// ---- Japan (3:2) ----
;(function () {
  const W = 30, H = 20
  let s = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${W} ${H}" width="${W}" height="${H}">`
  s += `<rect width="${W}" height="${H}" fill="#FFFFFF"/>`
  s += `<circle cx="${W / 2}" cy="${H / 2}" r="6" fill="#BC002D"/>`
  s += `</svg>`
  flags['jp'] = s
})()

// ---- South Korea (3:2) ----
;(function () {
  const W = 30, H = 20
  const cx = W / 2, cy = H / 2, R = 6
  let s = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${W} ${H}" width="${W}" height="${H}">`
  s += `<rect width="${W}" height="${H}" fill="#FFFFFF"/>`
  // taegeuk: red top half + blue bottom half
  s += `<path d="M${cx - R},${cy} A${R},${R} 0 0 1 ${cx + R},${cy} Z" fill="#CD2E3A"/>`
  s += `<path d="M${cx - R},${cy} A${R},${R} 0 0 0 ${cx + R},${cy} Z" fill="#0047A0"/>`
  // small white S separators
  s += `<path d="M${cx},${cy - R * 0.62} A${R * 0.55},${R * 0.55} 0 0 1 ${cx},${cy} L${cx},${cy} A${R * 0.55},${R * 0.55} 0 0 0 ${cx},${cy - R * 0.62} Z" fill="#fff"/>`
  s += `<path d="M${cx},${cy + R * 0.62} A${R * 0.55},${R * 0.55} 0 0 0 ${cx},${cy} L${cx},${cy} A${R * 0.55},${R * 0.55} 0 0 1 ${cx},${cy + R * 0.62} Z" fill="#fff"/>`
  // 4 trigrams (simplified: groups of 3 bars)
  const bar = (x, y, w, h) => `<rect x="${x}" y="${y}" width="${w}" height="${h}" fill="#000"/>`
  const trig = (x, y, vertical) => {
    if (vertical) {
      // 3 vertical bars
      return bar(x, y, 1.2, 4) + bar(x + 2.2, y, 1.2, 4) + bar(x + 4.4, y, 1.2, 4)
    } else {
      // 3 horizontal bars
      return bar(x, y, 4, 1.2) + bar(x, y + 2.2, 4, 1.2) + bar(x, y + 4.4, 4, 1.2)
    }
  }
  s += trig(3.2, 2.0, true)   // top-left-ish
  s += trig(22.8, 2.0, true)  // top-right-ish
  s += trig(3.2, 13.8, true)  // bottom-left-ish
  s += trig(22.8, 13.8, true) // bottom-right-ish
  s += `</svg>`
  flags['kr'] = s
})()

for (const [k, svg] of Object.entries(flags)) {
  fs.writeFileSync(path.join(OUT, k + '.svg'), svg, 'utf8')
  console.log('wrote', k + '.svg', svg.length, 'bytes')
}
console.log('DONE', Object.keys(flags).join(','))
