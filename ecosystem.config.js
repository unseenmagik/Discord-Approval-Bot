// pm2 process file. Start with: pm2 start ecosystem.config.js && pm2 save
module.exports = {
  apps: [
    {
      name: "approval-bot",
      script: "bot.py",
      interpreter: "./.venv/bin/python",
      cwd: __dirname,
      autorestart: true,
      restart_delay: 5000,
      max_restarts: 20,
      env: { PYTHONUNBUFFERED: "1" },
    },
  ],
};
