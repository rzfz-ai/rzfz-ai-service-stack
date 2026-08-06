# Komodo

Komodo is the container-infrastructure management and monitoring UI in your
stack. It gives you a single dashboard over the box's Docker environment — see
which containers and stacks are running, inspect their state and resource use,
follow logs, and start, stop or redeploy services.

## How to reach it

Open [https://admin.<domain>](https://admin.<domain>) and sign in with your
razzfazz.ai single sign-on. (The monitoring UI lives on the `admin` subdomain.)

## First steps

1. Open the **Servers / Resources** view to see the containers and stacks that
   make up your box.
2. Select a container to inspect its **status, resource usage and logs** — handy
   when a module misbehaves.
3. Use the **actions** (restart / redeploy) on a resource when you need to bring
   a service back or apply a change.

Komodo is an operator tool; day-to-day stack changes are normally made with the
`rzfz` CLI and the Configuration Portal (`https://config.<domain>`), with Komodo
providing the live view and manual controls.

## Full upstream documentation

For the complete concepts, configuration and API reference, see the official
Komodo documentation: [https://komo.do/docs/intro](https://komo.do/docs/intro)
