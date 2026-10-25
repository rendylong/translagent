/* applyRoute 断言向量(spec WP1-2 / Task 3):路由解析是纯函数——输入 hash 字符串,
   输出 {type:'empty'|'job'|'lib', id?},不碰 DOM,可脱离浏览器单独断言。
   提取方式沿用 live_page_selftest 的 splitSentences 先例:按花括号配平从 index.html
   抠出单个函数,不 eval 整文件(index.html 内联脚本顶层就有 DOM/fetch 依赖)。
   运行(本机无 node,用 Electron 的 node 模式):
     ELECTRON_RUN_AS_NODE=1 .tools/electron/Electron.app/Contents/MacOS/Electron \
       transcriber/tests/test_ui_route_vectors.mjs
   全绿打印 ROUTE_VECTORS_OK;任何差异非零退出并打印第一条失败向量。 */
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { deepStrictEqual } from 'node:assert';

const here = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(join(here, '..', 'static', 'index.html'), 'utf8');

/* 花括号配平抠函数(与 live_page_selftest.extract_function 同款):
   前提是函数体内没有含裸花括号的字符串/正则字面量——applyRoute 满足。 */
function extractFunction(text, name) {
  const m = text.match(new RegExp('function\\s+' + name + '\\s*\\('));
  if (!m) throw new Error('index.html 缺少函数 ' + name + '(未实现或被改名)');
  const start = text.indexOf('{', m.index + m[0].length - 1);
  let depth = 0;
  for (let i = start; i < text.length; i++) {
    if (text[i] === '{') depth++;
    else if (text[i] === '}') {
      depth--;
      if (depth === 0) return text.slice(m.index, i + 1);
    }
  }
  throw new Error(name + ' 花括号未配平(源码残缺?)');
}

const applyRoute = new Function(
  'return (' + extractFunction(src, 'applyRoute') + ');')();

/* 断言向量。期望对象断到完整形状(deepStrictEqual),防 parse 多带/少带字段。
   - '' 与 '#':启动无 hash / 根路径 → 空态
   - 合法 job id:后端形状 time.strftime('%Y%m%d-%H%M%S') + '_' + uuid4hex[:4]
   - '#speakers':说话人库
   - 中文/空格/点号/斜杠:不匹配 ^[\w-]+$ → 落空态(spec:坏 hash 不报错)
   - 超长(>80):防御性落空态
   - 大写/下划线/连字符:合法 id 字符集,必须放行 */
const JOB_ID = '20261024-153000_a1b2';
const VECTORS = [
  ['', { type: 'empty' }],
  ['#', { type: 'empty' }],
  ['#speakers', { type: 'lib' }],
  ['#' + JOB_ID, { type: 'job', id: JOB_ID }],
  ['#ABC-1_2', { type: 'job', id: 'ABC-1_2' }],
  ['#中文', { type: 'empty' }],
  ['#abc def', { type: 'empty' }],
  ['#abc.def', { type: 'empty' }],
  ['#a/b', { type: 'empty' }],
  ['#' + 'x'.repeat(200), { type: 'empty' }],
];

let failed = 0;
for (const [input, want] of VECTORS) {
  try {
    deepStrictEqual(applyRoute(input), want);
  } catch (e) {
    failed++;
    console.error('FAIL applyRoute(%j):\n  期望 %j\n  实际 %j', input, want,
      applyRoute(input));
    break;   // 首个失败即停,输出保持可读
  }
}
if (failed) process.exit(1);
console.log('ROUTE_VECTORS_OK applyRoute %d/%d 向量全过', VECTORS.length,
  VECTORS.length);
