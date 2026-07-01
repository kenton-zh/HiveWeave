const WebSocket = require("ws")

const socket = new WebSocket("ws://localhost:4000/socket/websocket")

let result = { connected: false, receivedMessages: [] }

socket.on("open", () => {
  result.connected = true
  // Phoenix.js v2 protocol
  socket.send(JSON.stringify({topic: "lobby:status", event: "phx_join", payload: {}, ref: "1"}))
  setTimeout(() => {
    socket.close()
    console.log(JSON.stringify(result))
  }, 3000)
})

socket.on("message", (data) => {
  try {
    const msg = JSON.parse(data.toString())
    result.receivedMessages.push(msg.event)
  } catch (e) {}
})

socket.on("error", (err) => {
  console.log("ERROR:", err.message)
})

socket.on("close", () => {
  console.log("CLOSED")
})
