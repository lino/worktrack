/**
 * AST and source craft hygiene validator for `frontend/src`, `frontend/tests`, and `frontend/scripts`.
 *
 * Enforces:
 * - File length <= 450 lines (unless `# fritz: allow-file-length=<max> reason="<why>"`)
 * - Function/method length <= 50 lines and McCabe cyclomatic complexity <= 10
 * - 100% TSDoc (`/** ... *\/`) coverage on modules, classes, functions, and methods in `src/`
 * - Zero hardcoded OS user home directory prefixes across `src/`, `tests/`, and `scripts/`
 */
import { readdirSync, readFileSync, statSync } from 'node:fs';
import { relative, resolve } from 'node:path';
import ts from 'typescript';

const FRONTEND_ROOT = resolve(import.meta.dirname, '..');
const SRC_DIR = resolve(FRONTEND_ROOT, 'src');
const TESTS_DIR = resolve(FRONTEND_ROOT, 'tests');
const SCRIPTS_DIR = resolve(FRONTEND_ROOT, 'scripts');
const MAX_FILE_LINES = 450;
const MAX_FUNCTION_LINES = 50;
const MAX_COMPLEXITY = 10;
const ALLOW_LENGTH_RE = /#\s*fritz:\s*allow-file-length=(\d+)\s+reason="([^"]+)"/;
const FORBIDDEN_PATH_PREFIXES = ['/' + 'Users/', '/' + 'home/', '~' + '/', 'C:\\' + 'Users\\'];

const BRANCH_SYNTAX_KINDS = new Set([
  ts.SyntaxKind.IfStatement,
  ts.SyntaxKind.ForStatement,
  ts.SyntaxKind.ForInStatement,
  ts.SyntaxKind.ForOfStatement,
  ts.SyntaxKind.WhileStatement,
  ts.SyntaxKind.DoStatement,
  ts.SyntaxKind.CaseClause,
  ts.SyntaxKind.CatchClause,
  ts.SyntaxKind.ConditionalExpression,
]);

const LOGICAL_OPERATOR_KINDS = new Set([
  ts.SyntaxKind.AmpersandAmpersandToken,
  ts.SyntaxKind.BarBarToken,
  ts.SyntaxKind.QuestionQuestionToken,
]);

/**
 * Recursively collect files matching extensions under `dir`.
 * @param {string} dir Root directory to scan.
 * @param {string[]} extensions File extensions to include.
 * @returns {string[]} Sorted absolute file paths.
 */
function collectFiles(dir, extensions) {
  const results = [];
  if (!statSync(dir, { throwIfNoEntry: false })) {
    return results;
  }
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = resolve(dir, entry.name);
    if (entry.isDirectory()) {
      results.push(...collectFiles(full, extensions));
    } else if (extensions.some((ext) => entry.name.endsWith(ext))) {
      results.push(full);
    }
  }
  return results.sort();
}

/**
 * Extract TypeScript script block from a `.vue` SFC or return raw `.ts` source.
 * @param {string} filePath Source file path.
 * @param {string} content Full file content.
 * @returns {string} TypeScript source text.
 */
function extractScriptSource(filePath, content) {
  if (!filePath.endsWith('.vue')) {
    return content;
  }
  const match = content.match(/<script[^>]*>([\s\S]*?)<\/script>/);
  return match ? match[1] : '';
}

/**
 * Check whether a TypeScript AST node is preceded by a `/** ... *\/` JSDoc comment.
 * @param {ts.Node} node AST node to inspect.
 * @param {string} sourceText Full TypeScript source text.
 * @returns {boolean} True when a JSDoc block comment precedes the node.
 */
function hasTsDocComment(node, sourceText) {
  const ranges = ts.getLeadingCommentRanges(sourceText, node.getFullStart()) ?? [];
  return ranges.some((range) => sourceText.slice(range.pos, range.end).startsWith('/**'));
}

/**
 * Check whether an AST node contributes a McCabe branch decision point.
 * @param {ts.Node} node AST node.
 * @returns {boolean} True when the node increments cyclomatic complexity.
 */
function isBranchNode(node) {
  if (BRANCH_SYNTAX_KINDS.has(node.kind)) {
    return true;
  }
  return ts.isBinaryExpression(node) && LOGICAL_OPERATOR_KINDS.has(node.operatorToken.kind);
}

/**
 * Compute McCabe cyclomatic complexity for a function-like AST node.
 * @param {ts.Node} root Function or method AST node.
 * @returns {number} Cyclomatic complexity count (base 1).
 */
function computeComplexity(root) {
  let complexity = 1;
  const visit = (node) => {
    if (isBranchNode(node)) {
      complexity += 1;
    }
    ts.forEachChild(node, visit);
  };
  if (root.body) {
    ts.forEachChild(root.body, visit);
  }
  return complexity;
}

/**
 * Check file-level line limit, path hygiene, and module TSDoc header.
 * @param {string} rel Relative file path.
 * @param {string} content Full file content.
 * @param {string} scriptText Extracted script text.
 * @param {boolean} requireTsDoc Whether module TSDoc is required.
 * @returns {string[]} File-level violation messages.
 */
function checkFileHeadersAndPaths(rel, content, scriptText, requireTsDoc) {
  const violations = [];
  const lines = content.split('\n');
  const allowMatch = content.match(ALLOW_LENGTH_RE);
  const maxLines = allowMatch ? Number(allowMatch[1]) : MAX_FILE_LINES;
  if (lines.length > maxLines) {
    violations.push(`${rel}: ${lines.length} lines exceeds limit ${maxLines}`);
  }
  for (const prefix of FORBIDDEN_PATH_PREFIXES) {
    if (content.includes(prefix)) {
      violations.push(`${rel}: contains forbidden hardcoded path prefix "${prefix}"`);
    }
  }
  if (requireTsDoc && !scriptText.trimStart().startsWith('/**')) {
    violations.push(`${rel}: missing module-level TSDoc comment (/** ... */)`);
  }
  return violations;
}

/**
 * Check function/method body statement line count and McCabe complexity.
 * @param {ts.FunctionDeclaration | ts.MethodDeclaration} node Callable AST node.
 * @param {ts.SourceFile} sourceFile Parsed TypeScript source file.
 * @param {string} rel Relative file path.
 * @param {string} name Symbol name.
 * @returns {string[]} Metric violation messages.
 */
function checkFunctionMetrics(node, sourceFile, rel, name) {
  if (!node.body) {
    return [];
  }
  const violations = [];
  const startLine = sourceFile.getLineAndCharacterOfPosition(node.body.getStart(sourceFile)).line;
  const endLine = sourceFile.getLineAndCharacterOfPosition(node.body.getEnd()).line;
  const stmtLines = Math.max(0, endLine - startLine - 1);
  if (stmtLines > MAX_FUNCTION_LINES) {
    violations.push(`${rel}:${name} has ${stmtLines} lines > ${MAX_FUNCTION_LINES}`);
  }
  const complexity = computeComplexity(node);
  if (complexity > MAX_COMPLEXITY) {
    violations.push(`${rel}:${name} has McCabe complexity ${complexity} > ${MAX_COMPLEXITY}`);
  }
  return violations;
}

/**
 * Check a single function, method, or class declaration node for TSDoc, length, and complexity.
 * @param {ts.Node} node AST node to inspect.
 * @param {ts.SourceFile} sourceFile Parsed TypeScript source file.
 * @param {string} scriptText Raw script source text.
 * @param {string} rel Relative file path.
 * @param {boolean} requireTsDoc Whether TSDoc comments are required.
 * @returns {string[]} Node-level violation messages.
 */
function checkDeclarationNode(node, sourceFile, scriptText, rel, requireTsDoc) {
  const isCallable = ts.isFunctionDeclaration(node) || ts.isMethodDeclaration(node);
  if (!isCallable && !ts.isClassDeclaration(node)) {
    return [];
  }
  const violations = [];
  const name = node.name ? node.name.getText(sourceFile) : '<anonymous>';
  if (requireTsDoc && !hasTsDocComment(node, scriptText)) {
    violations.push(`${rel}: missing TSDoc on ${name}`);
  }
  if (isCallable) {
    violations.push(...checkFunctionMetrics(node, sourceFile, rel, name));
  }
  return violations;
}

/**
 * Validate a single file for line count, path hygiene, TSDoc, and function complexity.
 * @param {string} filePath Absolute file path.
 * @param {boolean} requireTsDoc Whether to enforce 100% TSDoc coverage.
 * @returns {string[]} Collected violation descriptions.
 */
function validateFile(filePath, requireTsDoc) {
  const rel = relative(FRONTEND_ROOT, filePath);
  const content = readFileSync(filePath, 'utf-8');
  const scriptText = extractScriptSource(filePath, content);
  const violations = checkFileHeadersAndPaths(rel, content, scriptText, requireTsDoc);
  const sourceFile = ts.createSourceFile(
    filePath,
    scriptText,
    ts.ScriptTarget.Latest,
    true,
    ts.ScriptKind.TS,
  );
  const inspectNode = (node) => {
    violations.push(...checkDeclarationNode(node, sourceFile, scriptText, rel, requireTsDoc));
    ts.forEachChild(node, inspectNode);
  };
  ts.forEachChild(sourceFile, inspectNode);
  return violations;
}

const srcFiles = collectFiles(SRC_DIR, ['.ts', '.vue']);
const testFiles = collectFiles(TESTS_DIR, ['.ts']);
const scriptFiles = collectFiles(SCRIPTS_DIR, ['.mjs', '.js', '.ts']);
const allViolations = [
  ...srcFiles.flatMap((file) => validateFile(file, true)),
  ...testFiles.flatMap((file) => validateFile(file, false)),
  ...scriptFiles.flatMap((file) => validateFile(file, true)),
];

if (allViolations.length > 0) {
  console.error('Frontend craft hygiene violations:\n' + allViolations.join('\n'));
  process.exit(1);
}
console.log(
  `Frontend craft hygiene verified across ${srcFiles.length} src, ${testFiles.length} test, and ${scriptFiles.length} script files.`,
);
