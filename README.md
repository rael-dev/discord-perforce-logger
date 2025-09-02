# Perforce → Discord Logger Bot

This bot is extending the functionality of [TibRib's Perforce-discord-botlogger](https://github.com/TibRib/Perforce-discord-botlogger).

This script integrates **Perforce (P4)** and **Helix Swarm (P4 Code Review)** with **Discord**.  
It automatically posts messages to Discord webhooks when:

- A **new P4 changelist submission** is detected.  
- A **new Swarm code review** is created, or when participants update their votes.  

The bot also **keeps review post information up to date** in Discord, including participant approvals/rejections and color-coded embed states.