import { createRouter, createWebHistory } from 'vue-router'
import { useSessionStore } from '@/stores/session'

export const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/login', name: 'login', component: () => import('@/pages/Login.vue'), meta: { public: true } },
    { path: '/', redirect: '/industries' },
    { path: '/industries', name: 'industries', component: () => import('@/pages/Industries.vue') },
    {
      path: '/i/:industryId',
      component: () => import('@/components/AppShell.vue'),
      children: [
        { path: '', name: 'workspace', component: () => import('@/pages/Workspace.vue') },
        { path: 'documents', name: 'documents', component: () => import('@/pages/Documents.vue') },
        { path: 'topics', name: 'topics', component: () => import('@/pages/Topic.vue') },
        { path: 'timeline', name: 'timeline', component: () => import('@/pages/Timeline.vue') },
        { path: 'evolution', name: 'evolution', component: () => import('@/pages/Evolution.vue') },
        { path: 'events/:eventId', name: 'event', component: () => import('@/pages/EventDetail.vue') },
        { path: 'qa', name: 'qa', component: () => import('@/pages/Conversations.vue') },
        { path: 'reports', name: 'reports', component: () => import('@/pages/Reports.vue') },
        { path: 'reviews', name: 'reviews', component: () => import('@/pages/Reviews.vue') },
        { path: 'settings', name: 'industry-settings', component: () => import('@/pages/Settings.vue') },
      ],
    },
    { path: '/runs', name: 'runs', component: () => import('@/pages/RunCenter.vue') },
    { path: '/account', name: 'account', component: () => import('@/pages/Settings.vue') },
  ],
})

router.beforeEach((to) => {
  const session = useSessionStore()
  if (!to.meta.public && !session.user) return { name: 'login' }
  if (to.meta.public && session.user && to.name === 'login') return { name: 'industries' }
})
