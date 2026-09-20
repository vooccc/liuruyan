export type AnimationType =
  | 'fade-in'
  | 'fade-out'
  | 'slide-in'
  | 'slide-out'
  | 'pulse'
  | 'typewriter'
  | 'bounce'

export interface AnimationConfig {
  type: AnimationType
  duration: number
  easing?: string
  delay?: number
  iterations?: number
}

export interface AnimationTarget {
  elementId: string
  config: AnimationConfig
}

class AnimationSystem {
  private activeAnimations: Map<string, Animation> = new Map()

  async animate(target: AnimationTarget): Promise<void> {
    const element = document.getElementById(target.elementId)
    if (!element) {
      console.warn(`[AnimationSystem] Element not found: ${target.elementId}`)
      return
    }

    const existing = this.activeAnimations.get(target.elementId)
    if (existing) {
      existing.cancel()
    }

    const animation = this.createAnimation(element, target.config)
    this.activeAnimations.set(target.elementId, animation)

    try {
      await animation.finished
    } catch {
      // Animation cancelled or failed
    } finally {
      this.activeAnimations.delete(target.elementId)
    }
  }

  private createAnimation(element: HTMLElement, config: AnimationConfig): Animation {
    const keyframes = this.getKeyframes(config.type)
    return element.animate(keyframes, {
      duration: config.duration,
      easing: config.easing || 'ease-out',
      delay: config.delay || 0,
      iterations: config.iterations || 1,
      fill: 'forwards',
    })
  }

  private getKeyframes(type: AnimationType): Keyframe[] {
    switch (type) {
      case 'fade-in':
        return [{ opacity: 0 }, { opacity: 1 }]
      case 'fade-out':
        return [{ opacity: 1 }, { opacity: 0 }]
      case 'slide-in':
        return [
          { transform: 'translateY(-10px)', opacity: 0 },
          { transform: 'translateY(0)', opacity: 1 },
        ]
      case 'slide-out':
        return [
          { transform: 'translateY(0)', opacity: 1 },
          { transform: 'translateY(-10px)', opacity: 0 },
        ]
      case 'pulse':
        return [
          { transform: 'scale(1)' },
          { transform: 'scale(1.05)' },
          { transform: 'scale(1)' },
        ]
      case 'bounce':
        return [
          { transform: 'translateY(0)' },
          { transform: 'translateY(-10px)' },
          { transform: 'translateY(0)' },
          { transform: 'translateY(-5px)' },
          { transform: 'translateY(0)' },
        ]
      default:
        return []
    }
  }

  cancel(elementId: string): void {
    const animation = this.activeAnimations.get(elementId)
    if (animation) {
      animation.cancel()
      this.activeAnimations.delete(elementId)
    }
  }

  cancelAll(): void {
    for (const animation of this.activeAnimations.values()) {
      animation.cancel()
    }
    this.activeAnimations.clear()
  }

  isAnimating(elementId: string): boolean {
    return this.activeAnimations.has(elementId)
  }
}

export const animationSystem = new AnimationSystem()
