// Offline, newline-delimited JSON -> SVG. No browser, CDN, or paid service.
const {mathjax} = require('mathjax-full/js/mathjax.js');
const {TeX} = require('mathjax-full/js/input/tex.js');
const {SVG} = require('mathjax-full/js/output/svg.js');
const {liteAdaptor} = require('mathjax-full/js/adaptors/liteAdaptor.js');
const {RegisterHTMLHandler} = require('mathjax-full/js/handlers/html.js');
require('mathjax-full/js/input/tex/ams/AmsConfiguration.js');
require('mathjax-full/js/input/tex/newcommand/NewcommandConfiguration.js');
const adaptor = liteAdaptor();
RegisterHTMLHandler(adaptor);
const document = mathjax.document('', {
  InputJax: new TeX({packages:['base','ams','newcommand'], maxBuffer:16384, maxMacros:1000}),
  OutputJax: new SVG({fontCache:'local'})
});
const lines = require('readline').createInterface({input:process.stdin});
lines.on('line', line => {
  try {
    const input = JSON.parse(line);
    const node = document.convert(input.tex, {display:input.display !== false});
    const svg = adaptor.innerHTML(node);
    if (svg.includes('data-mjx-error')) throw new Error('Unsupported TeX');
    process.stdout.write(JSON.stringify({svg})+'\n');
  } catch (error) { process.stdout.write(JSON.stringify({error:String(error.message)})+'\n'); }
});
