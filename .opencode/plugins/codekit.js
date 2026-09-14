import path from 'path';
import fs from 'fs';
import { fileURLToPath } from 'url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
// 技能目录相对本文件定位：.opencode/plugins/ → skills/
const skillsSourceDir = path.resolve(__dirname, '../../skills');

function parseSkillFile(content) {
  const match = content.match(/^---\n([\s\S]*?)\n---\n([\s\S]*)$/);
  if (!match) return null;
  const fm = match[1];
  const body = match[2];
  const fields = {};
  for (const line of fm.split('\n')) {
    const idx = line.indexOf(':');
    if (idx > 0) {
      const key = line.slice(0, idx).trim();
      const value = line.slice(idx + 1).trim().replace(/^["']|["']$/g, '');
      if (key && !(key in fields)) fields[key] = value;
    }
  }
  // Skill folder name is the command name (e.g. skills/initialize/SKILL.md → initialize)
  return {
    description: fields.description || '',
    template: body.trim() + '\n\n$ARGUMENTS\n',
  };
}

export const codekitPlugin = async ({ client }) => {
  const config = { command: {} };

  try {
    const entries = fs
      .readdirSync(skillsSourceDir, { withFileTypes: true })
      .filter((e) => e.isDirectory() && fs.existsSync(path.join(skillsSourceDir, e.name, 'SKILL.md')));
    for (const entry of entries) {
      const content = fs.readFileSync(path.join(skillsSourceDir, entry.name, 'SKILL.md'), 'utf8');
      const parsed = parseSkillFile(content);
      if (parsed) {
        config.command[entry.name] = parsed;
      }
    }
    await client.app.log({
      body: { service: 'CodeKit', level: 'info', message: `Registered ${Object.keys(config.command).length} commands via config hook` },
    });
  } catch (err) {
    await client.app.log({
      body: { service: 'CodeKit', level: 'warn', message: `Failed to register commands: ${err.message}` },
    });
  }

  return {
    config: async (c) => {
      Object.assign(c.command || {}, config.command);
    },
  };
};
