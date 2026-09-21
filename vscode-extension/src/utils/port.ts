// 端口探测 —— OS 分配空闲端口（port=0）。
//
// 设计依据：_docs/ide_integration_principles.md 铁律 2「插件起本地 HTTP server，端口随机」。
// 避免写死端口跟其他软件抢，且多开 IDE 实例时各占各的。

import * as net from 'net'

export async function findFreePort(): Promise<number> {
  return new Promise((resolve, reject) => {
    const server = net.createServer()
    server.unref()
    server.on('error', reject)
    server.listen(0, '127.0.0.1', () => {
      const address = server.address()
      if (address && typeof address === 'object') {
        const port = address.port
        server.close(() => resolve(port))
      } else {
        server.close(() => reject(new Error('Failed to allocate port: address() returned unexpected value')))
      }
    })
  })
}
